"""Extraction des diaporamas photo TikTok (API tikwm, repli gallery-dl)."""

from __future__ import annotations

import asyncio
import io
import logging
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

import httpx
from PIL import Image

from fripe.models import SlidePost

log = logging.getLogger(__name__)

TIKWM_API = "https://www.tikwm.com/api/"

# tikwm tolere environ 1 requete/seconde et 5000/jour par IP.
_MIN_INTERVAL_S = 1.1
_HTTP_TIMEOUT_S = 20.0
_GALLERY_DL_TIMEOUT_S = 120.0
_MAX_CONCURRENT_DOWNLOADS = 4

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_API_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}
_IMAGE_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "image/avif,image/webp,image/jpeg,image/png,*/*;q=0.8",
    "Referer": "https://www.tiktok.com/",
}

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
_JPEG_MAGIC = b"\xff\xd8\xff"
_FILE_SCHEME = "file://"

TIKTOK_URL_RE = re.compile(
    r"""https?://(?:
          v[mt]\.tiktok\.com/[A-Za-z0-9._-]+
        | (?:www\.|m\.)?tiktok\.com/t/[A-Za-z0-9._-]+
        | (?:www\.|m\.)?tiktok\.com/@[A-Za-z0-9._-]+/(?:photo|video)/\d+
    )(?:[/?\#][^\s<>"']*)?""",
    re.IGNORECASE | re.VERBOSE,
)

_POST_ID_RE = re.compile(r"/(?:photo|video)/(\d+)")
_TRAILING_PUNCT = ".,;:!?)]}\"'»>"

# Serialise les appels tikwm entre coroutines concurrentes.
_TIKWM_LOCK = asyncio.Lock()
_last_tikwm_call = 0.0

# Dossiers temporaires crees par le repli gallery-dl, vides des que
# download_slides a recopie les slides.
_TEMP_DIRS: set[Path] = set()


class TikTokError(Exception):
    """Echec d'extraction TikTok, porteur d'un message affichable a l'utilisateur."""

    default_message_fr = (
        "Je n'ai pas réussi à récupérer les images de ce lien TikTok."
    )

    def __init__(self, message: str = "", *, user_message_fr: str | None = None) -> None:
        super().__init__(message or self.default_message_fr)
        self.user_message_fr = user_message_fr or self.default_message_fr


class NotATikTokUrl(TikTokError):
    default_message_fr = (
        "Je ne vois pas de lien TikTok ici. Envoie-moi le lien de partage "
        "d'un diaporama photo."
    )


class VideoPost(TikTokError):
    default_message_fr = (
        "Ce lien est une video, je ne sais traiter que les diaporamas photo "
        "(les posts a plusieurs images)."
    )


class ExtractorDown(TikTokError):
    default_message_fr = (
        "Le service qui récupère les images TikTok ne répond pas. "
        "Réessaie dans quelques minutes."
    )


def find_tiktok_url(text: str) -> str | None:
    """Renvoie la premiere URL TikTok d'un message Telegram, sinon None."""
    match = TIKTOK_URL_RE.search(text or "")
    if match is None:
        return None
    return match.group(0).rstrip(_TRAILING_PUNCT) or None


async def fetch_slides(share_url: str) -> SlidePost:
    """Resout un lien TikTok en un SlidePost (URLs distantes ou `file://` locales).

    Les URLs renvoyees par tikwm sont signees et expirent en quelques heures :
    il faut appeler download_slides immediatement apres.
    """
    url = find_tiktok_url(share_url)
    if url is None:
        raise NotATikTokUrl(f"aucune URL TikTok dans {share_url!r}")

    reasons: list[str] = []
    try:
        return await _fetch_via_tikwm(url)
    except VideoPost:
        raise
    except Exception as exc:
        reasons.append(f"tikwm: {exc}")
        log.warning("tikwm a echoue pour %s : %s", url, exc)

    try:
        return await _fetch_via_gallery_dl(url)
    except VideoPost:
        raise
    except Exception as exc:
        reasons.append(f"gallery-dl: {exc}")
        log.warning("gallery-dl a echoue pour %s : %s", url, exc)

    raise ExtractorDown(f"aucun extracteur n'a pu traiter {url} ({' | '.join(reasons)})")


async def download_slides(
    post: SlidePost, dest: Path, *, max_slides: int = 12
) -> list[Path]:
    """Telecharge les slides dans `dest` sous la forme `01.jpg`, `02.jpg`, ...

    Les echecs isoles sont ignores ; ExtractorDown n'est levee que si aucune
    slide n'a pu etre recuperee.
    """
    urls = [u for u in post.image_urls if u][: max(0, max_slides)]
    if not urls:
        raise ExtractorDown(f"post {post.post_id} sans image a telecharger")

    dest.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_DOWNLOADS)

    try:
        async with httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT_S, follow_redirects=True, headers=_IMAGE_HEADERS
        ) as client:
            results = await asyncio.gather(
                *(
                    _download_one(client, semaphore, url, dest / f"{index:02d}.jpg")
                    for index, url in enumerate(urls, start=1)
                )
            )
    finally:
        release_local_copies(post)

    paths = [path for path in results if path is not None]
    if not paths:
        raise ExtractorDown(f"aucune slide telechargeable pour le post {post.post_id}")

    log.info("%d/%d slides telechargees dans %s", len(paths), len(urls), dest)
    return paths


def release_local_copies(post: SlidePost) -> None:
    """Supprime le dossier temporaire eventuel derriere les URLs `file://` du post.

    A appeler si le post est abandonne sans passer par download_slides.
    """
    for root in list(_TEMP_DIRS):
        prefix = root.as_uri().rstrip("/") + "/"
        if not any(url.startswith(prefix) for url in post.image_urls):
            continue
        _TEMP_DIRS.discard(root)
        shutil.rmtree(root, ignore_errors=True)


async def _throttle_tikwm() -> None:
    global _last_tikwm_call
    async with _TIKWM_LOCK:
        wait = _MIN_INTERVAL_S - (time.monotonic() - _last_tikwm_call)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_tikwm_call = time.monotonic()


async def _fetch_via_tikwm(url: str) -> SlidePost:
    await _throttle_tikwm()

    async with httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT_S, follow_redirects=True, headers=_API_HEADERS
    ) as client:
        response = await client.get(TIKWM_API, params={"url": url})
    response.raise_for_status()

    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"reponse tikwm inattendue ({type(payload).__name__})")

    code = payload.get("code")
    if code != 0:
        raise ValueError(f"tikwm code={code!r} msg={payload.get('msg')!r}")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("tikwm code=0 sans objet 'data'")

    images = [str(item) for item in (data.get("images") or []) if item]
    if not images:
        raise VideoPost(f"post {url} sans liste 'images' (probablement une video)")

    return SlidePost(
        post_id=str(data.get("id") or _post_id_from_url(url) or "inconnu"),
        title=str(data.get("title") or "").strip(),
        image_urls=images,
    )


async def _fetch_via_gallery_dl(url: str) -> SlidePost:
    """Repli : gallery-dl scrappe tiktok.com et depose les slides sur le disque.

    Depuis une IP de datacenter il echoue souvent faute de cookies : c'est un
    cas normal, pas une anomalie.
    """
    try:
        import gallery_dl  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("extra 'gallerydl' absent") from exc

    executable = shutil.which("gallery-dl")
    command = [executable] if executable else [sys.executable, "-m", "gallery_dl"]

    tmpdir = Path(tempfile.mkdtemp(prefix="fripe-gdl-"))
    _TEMP_DIRS.add(tmpdir)
    command += [
        "--no-colors",
        "-o",
        "extractor.tiktok.audio=false",
        "-D",
        str(tmpdir),
        url,
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(), timeout=_GALLERY_DL_TIMEOUT_S
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise RuntimeError(f"gallery-dl a depasse {_GALLERY_DL_TIMEOUT_S:.0f}s") from None

        files = sorted(
            (p for p in tmpdir.rglob("*") if p.is_file() and p.suffix.lower() in _IMAGE_SUFFIXES),
            key=_natural_key,
        )
        if not files:
            if any(p.suffix.lower() == ".mp4" for p in tmpdir.rglob("*")):
                raise VideoPost(f"gallery-dl n'a trouve qu'une video pour {url}")
            detail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
            raise RuntimeError(
                f"gallery-dl code={process.returncode} sans image "
                f"({detail[-1] if detail else 'pas de sortie'})"
            )
        if process.returncode:
            log.warning(
                "gallery-dl a rendu le code %s mais %d image(s) : on continue",
                process.returncode,
                len(files),
            )
    except BaseException:
        _TEMP_DIRS.discard(tmpdir)
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise

    return SlidePost(
        post_id=str(_post_id_from_url(url) or files[0].stem),
        title="",
        image_urls=[p.as_uri() for p in files],
    )


async def _download_one(
    client: httpx.AsyncClient, semaphore: asyncio.Semaphore, url: str, target: Path
) -> Path | None:
    async with semaphore:
        try:
            if url.startswith(_FILE_SCHEME):
                data = await asyncio.to_thread(_file_uri_to_path(url).read_bytes)
            else:
                response = await client.get(url)
                response.raise_for_status()
                data = response.content
            if not data:
                raise ValueError("contenu vide")
            await asyncio.to_thread(_write_jpeg, data, target)
        except Exception as exc:
            log.warning("slide %s ignoree (%s) : %s", target.name, url[:120], exc)
            return None
    return target


def _write_jpeg(data: bytes, target: Path) -> None:
    """gallery-dl peut livrer du webp ou du png : on normalise en JPEG."""
    if data.startswith(_JPEG_MAGIC):
        target.write_bytes(data)
        return
    with Image.open(io.BytesIO(data)) as image:
        image.convert("RGB").save(target, format="JPEG", quality=90)


def _file_uri_to_path(uri: str) -> Path:
    return Path(url2pathname(urlparse(uri).path))


def _post_id_from_url(url: str) -> str | None:
    match = _POST_ID_RE.search(url)
    return match.group(1) if match else None


def _natural_key(path: Path) -> tuple[int | str, ...]:
    # "10.jpg" doit venir apres "9.jpg" : les nombres sont compares en tant que nombres.
    parts = re.split(r"(\d+)", path.name)
    return tuple(int(part) if part.isdigit() else part.lower() for part in parts)
