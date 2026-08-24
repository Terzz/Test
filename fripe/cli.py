"""Outil en ligne de commande : tester chaque etape sans passer par Telegram.

    python -m fripe.cli slides   https://vm.tiktok.com/XXXX/
    python -m fripe.cli vinted   "veste cuir marron" --catalog 1908 --color 2
    python -m fripe.cli analyze  https://vm.tiktok.com/XXXX/
    python -m fripe.cli llm-ping
    python -m fripe.cli run      https://vm.tiktok.com/XXXX/
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import httpx

from fripe.config import Config, ConfigError, load_config, setup_logging
from fripe.llm import build_backend
from fripe.pipeline import Deps, process_link
from fripe.rerank import build_reranker
from fripe.tiktok import download_slides, fetch_slides, find_tiktok_url
from fripe.vinted import VintedClient
from fripe.vision import analyze_slides


def _build_deps(cfg: Config) -> Deps:
    http = httpx.AsyncClient(timeout=20.0, follow_redirects=True)
    backend = build_backend(cfg)
    return Deps(
        vinted=VintedClient(cookie_cache=cfg.data_dir / "vinted_cookies.json"),
        backend=backend,
        reranker=build_reranker(cfg, backend, http),
        http=http,
    )


async def _close_deps(deps: Deps) -> None:
    await deps.vinted.close()
    await deps.http.aclose()


async def cmd_slides(cfg: Config, args: argparse.Namespace) -> int:
    url = find_tiktok_url(args.url) or args.url
    post = await fetch_slides(url)
    dest = cfg.data_dir / "debug" / "slides" / post.post_id
    paths = await download_slides(post, dest)
    print(f"post {post.post_id} — {len(paths)} slide(s)")
    for path in paths:
        print(f"  {path}")
    return 0


async def cmd_vinted(cfg: Config, args: argparse.Namespace) -> int:
    client = VintedClient(cookie_cache=cfg.data_dir / "vinted_cookies.json")
    try:
        items = await client.search(
            args.query,
            catalog_ids=args.catalog or None,
            color_ids=args.color or None,
            brand_ids=args.brand or None,
            price_to=args.price_to,
            per_page=args.limit,
        )
    finally:
        await client.close()

    print(f"{len(items)} annonce(s) pour {args.query!r}")
    for item in items:
        details = " · ".join(filter(None, [item.brand_title, item.size_title, item.status]))
        print(f"  {item.price_label():>9}  {item.title[:45]:45}  {details}")
        print(f"             {item.url}")
    return 0


async def cmd_analyze(cfg: Config, args: argparse.Namespace) -> int:
    deps = _build_deps(cfg)
    try:
        url = find_tiktok_url(args.url) or args.url
        post = await fetch_slides(url)
        dest = cfg.data_dir / "debug" / "slides" / post.post_id
        paths = await download_slides(post, dest)
        result = await analyze_slides(paths, deps.backend, cfg.analysis_model)
    finally:
        await _close_deps(deps)
    print(json.dumps(result.model_dump(), indent=2, ensure_ascii=False))
    return 0


async def cmd_llm_ping(cfg: Config, _: argparse.Namespace) -> int:
    deps = _build_deps(cfg)
    try:
        payload = await deps.backend.analyze_slides(
            [],
            system_prompt="Tu es un service de test. Tu reponds uniquement en JSON.",
            user_text='Reponds exactement ceci et rien d\'autre : {"ok": true}',
            model=cfg.analysis_model,
        )
    finally:
        await _close_deps(deps)
    print(f"backend={cfg.llm_backend} modele={cfg.analysis_model} -> {payload}")
    return 0


async def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    deps = _build_deps(cfg)

    async def progress(message: str) -> None:
        print(f"… {message}")

    try:
        results = await process_link(args.url, cfg, deps, progress)
    finally:
        await _close_deps(deps)

    for result in results:
        print(f"\n=== {result.garment.label_fr} ===")
        print(f"    requetes : {', '.join(result.garment.queries_fr)}")
        if result.note_fr:
            print(f"    note     : {result.note_fr}")
        if not result.items:
            print("    aucune annonce")
        for position, item in enumerate(result.items, start=1):
            print(f"    {position}. {item.price_label():>9}  {item.title[:45]}")
            print(f"       {item.url}")
    return 0


def _int_list(value: str) -> list[int]:
    return [int(part) for part in value.split(",") if part.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fripe", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    slides = sub.add_parser("slides", help="telecharge les images d'un diaporama TikTok")
    slides.add_argument("url")
    slides.set_defaults(func=cmd_slides)

    vinted = sub.add_parser("vinted", help="recherche brute sur Vinted")
    vinted.add_argument("query")
    vinted.add_argument("--catalog", type=_int_list, help="ex: 1908 ou 1037,1908")
    vinted.add_argument("--color", type=_int_list, help="ex: 1 (noir) ou 2,4")
    vinted.add_argument("--brand", type=_int_list, help="ex: 53 (Nike)")
    vinted.add_argument("--price-to", type=int, dest="price_to")
    vinted.add_argument("--limit", type=int, default=20)
    vinted.set_defaults(func=cmd_vinted)

    analyze = sub.add_parser("analyze", help="analyse les vetements d'un TikTok (JSON)")
    analyze.add_argument("url")
    analyze.set_defaults(func=cmd_analyze)

    ping = sub.add_parser("llm-ping", help="verifie l'acces au modele")
    ping.set_defaults(func=cmd_llm_ping)

    run = sub.add_parser("run", help="chaine complete, sans Telegram")
    run.add_argument("url")
    run.set_defaults(func=cmd_run)

    return parser


def main() -> None:
    setup_logging()
    args = build_parser().parse_args()
    try:
        cfg = load_config(require_telegram=False)
    except ConfigError as exc:
        print(f"Configuration invalide : {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    try:
        raise SystemExit(asyncio.run(args.func(cfg, args)))
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
