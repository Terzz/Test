"""Orchestration : d'un lien TikTok aux annonces Vinted classees."""

from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

from fripe.config import Config
from fripe.llm import LLMBackend, LLMError
from fripe.models import Garment, GarmentResult, VintedItem
from fripe.rerank import Reranker
from fripe.tiktok import (
    NotATikTokUrl,
    TikTokError,
    download_slides,
    fetch_slides,
    find_tiktok_url,
)
from fripe.vinted import VintedClient, VintedError
from fripe.vision import analyze_slides, crop_garment

log = logging.getLogger(__name__)

ProgressFn = Callable[[str], Awaitable[None]]

# Au-dela, le re-classement coute plus qu'il ne rapporte : les annonces suivantes
# sont deja tres loin de la requete.
MAX_CANDIDATES = 30
# Seuils de l'echelle d'elargissement.
ENOUGH_RESULTS = 8
MIN_RESULTS = 3


@dataclass
class Deps:
    """Ressources partagees, construites une fois par le bot ou la CLI."""

    vinted: VintedClient
    backend: LLMBackend
    reranker: Reranker
    http: httpx.AsyncClient


async def _noop_progress(_: str) -> None:
    return None


async def _safe_progress(on_progress: ProgressFn, message: str) -> None:
    """Un message de progression perdu ne doit jamais interrompre la recherche."""
    try:
        await on_progress(message)
    except Exception:
        log.debug("message de progression non delivre", exc_info=True)


def _merge_round_robin(result_lists: Sequence[Sequence[VintedItem]]) -> list[VintedItem]:
    """Entrelace les listes pour equilibrer les variantes, sans doublon d'annonce."""
    merged: list[VintedItem] = []
    seen: set[int] = set()
    for rank in range(max((len(lst) for lst in result_lists), default=0)):
        for lst in result_lists:
            if rank >= len(lst):
                continue
            item = lst[rank]
            if item.id in seen:
                continue
            seen.add(item.id)
            merged.append(item)
    return merged


async def _run_queries(
    queries: Sequence[str],
    deps: Deps,
    *,
    catalog_ids: list[int] | None,
    color_ids: list[int] | None,
    brand_ids: list[int] | None,
    price_to: int | None,
) -> list[VintedItem]:
    lists: list[list[VintedItem]] = []
    for query in queries:
        try:
            items = await deps.vinted.search(
                query,
                catalog_ids=catalog_ids,
                color_ids=color_ids,
                brand_ids=brand_ids,
                price_to=price_to,
            )
        except VintedError:
            raise
        except Exception:
            log.warning("recherche Vinted en echec pour %r", query, exc_info=True)
            continue
        log.info("Vinted %r -> %d annonces", query, len(items))
        lists.append(items)
    return _merge_round_robin(lists)


async def _search_garment(
    garment: Garment, cfg: Config, deps: Deps
) -> tuple[list[VintedItem], str | None]:
    """Cherche un vetement en relachant progressivement les filtres."""
    queries = garment.queries_fr or [garment.label_fr]
    catalog_ids = [garment.catalog_id] if garment.catalog_id else None
    brand_ids = [garment.brand_id] if garment.brand_id else None

    pooled = await _run_queries(
        queries,
        deps,
        catalog_ids=catalog_ids,
        color_ids=garment.color_ids or None,
        brand_ids=brand_ids,
        price_to=cfg.price_to,
    )
    if len(pooled) >= ENOUGH_RESULTS:
        return pooled, None

    note: str | None = None

    if garment.color_ids:
        widened = await _run_queries(
            queries[:1],
            deps,
            catalog_ids=catalog_ids,
            color_ids=None,
            brand_ids=brand_ids,
            price_to=cfg.price_to,
        )
        pooled = _merge_round_robin([pooled, widened])
        note = "recherche élargie : couleur ignorée"

    if len(pooled) >= MIN_RESULTS:
        return pooled, note

    widened = await _run_queries(
        queries[:1],
        deps,
        catalog_ids=None,
        color_ids=None,
        brand_ids=None,
        price_to=cfg.price_to,
    )
    pooled = _merge_round_robin([pooled, widened])
    if widened:
        note = "recherche élargie : filtres ignorés"
    return pooled, note


async def _rank_garment(
    garment: Garment,
    candidates: list[VintedItem],
    slide_paths: list[Path],
    cfg: Config,
    deps: Deps,
) -> list[VintedItem]:
    if len(candidates) <= 1:
        return candidates

    index = min(max(garment.slide_index, 1), len(slide_paths)) - 1
    source = crop_garment(slide_paths[index], garment.bbox, label="Image 1:")

    try:
        ranked = await deps.reranker.rank(source, candidates[:MAX_CANDIDATES])
    except Exception:
        # Le classement visuel est un confort : en cas de pepin on garde
        # l'ordre de pertinence de Vinted.
        log.warning("re-classement en echec pour %s", garment.label_fr, exc_info=True)
        ranked = candidates

    return ranked[: cfg.max_results]


async def process_link(
    url: str,
    cfg: Config,
    deps: Deps,
    on_progress: ProgressFn | None = None,
) -> list[GarmentResult]:
    """Chaine complete : lien TikTok -> liste d'annonces par vetement.

    Leve NotATikTokUrl / TikTokError / LLMError / VintedError, toutes porteuses
    d'un message francais destine a l'utilisateur.
    """
    progress = on_progress or _noop_progress
    started = time.monotonic()

    share_url = find_tiktok_url(url)
    if not share_url:
        raise NotATikTokUrl("aucun lien TikTok dans le message")

    post = await fetch_slides(share_url)
    slides_dir = cfg.data_dir / "slides" / post.post_id
    try:
        slide_paths = await download_slides(post, slides_dir)
        await _safe_progress(
            progress, f"📸 {len(slide_paths)} photo(s) récupérée(s), j'analyse la tenue…"
        )

        analysis = await analyze_slides(slide_paths, deps.backend, cfg.analysis_model)
        garments = analysis.garments
        if not garments:
            await _safe_progress(progress, "🤔 Je n'ai reconnu aucun vêtement sur ces photos.")
            return []

        labels = ", ".join(g.label_fr for g in garments)
        await _safe_progress(
            progress, f"👀 {len(garments)} pièce(s) repérée(s) : {labels}\nJe cherche sur Vinted…"
        )

        results: list[GarmentResult] = []
        for position, garment in enumerate(garments, start=1):
            await _safe_progress(
                progress,
                f"🔎 ({position}/{len(garments)}) {garment.label_fr}…",
            )
            candidates, note = await _search_garment(garment, cfg, deps)
            if not candidates:
                results.append(GarmentResult(garment=garment, items=[], note_fr=note))
                continue
            items = await _rank_garment(garment, candidates, slide_paths, cfg, deps)
            results.append(GarmentResult(garment=garment, items=items, note_fr=note))

        log.info(
            "pipeline termine en %.1fs : %d vetement(s), %d annonce(s)",
            time.monotonic() - started,
            len(results),
            sum(len(r.items) for r in results),
        )
        return results
    finally:
        shutil.rmtree(slides_dir, ignore_errors=True)


__all__ = ["Deps", "ProgressFn", "process_link"]
