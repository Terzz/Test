"""Preparation des images et lecture des slides par le modele visuel."""

from __future__ import annotations

import asyncio
import base64
import io
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image, ImageOps
from pydantic import ValidationError

from fripe.catalog import BRAND_IDS_BY_NAME, BRANDS
from fripe.models import AnalysisResult, BBox, Garment, ImagePart
from fripe.prompts import ANALYSIS_SYSTEM, analysis_user_text

if TYPE_CHECKING:
    from fripe.llm import LLMBackend

log = logging.getLogger(__name__)

__all__ = [
    "VisionError",
    "analyze_slides",
    "crop_garment",
    "encode_image",
    "prep_slide",
    "prep_thumbnail",
]

# Le cout en tokens d'une image vaut ceil(l/28)*ceil(h/28) : 336 px -> ~144
# tokens, 1024 px -> ~1350. On paye la finesse la ou elle sert.
SLIDE_MAX_SIDE = 1024
CROP_MAX_SIDE = 512
THUMBNAIL_MAX_SIDE = 336
JPEG_QUALITY = 85

# En dessous, le crop ne montre plus rien d'identifiable.
MIN_CROP_PX = 24

_CONFIDENCES = {"high", "medium", "low"}
_LIST_KEYS = ("garments", "vetements", "items", "results")
# Memes alias que _normalize : ce qui est reconnu dans une entree doit aussi
# suffire a reconnaitre un vetement livre seul, sans liste autour.
_ENTRY_KEYS = ("label_fr", "label", "nom", "name", "queries_fr", "queries")


class VisionError(Exception):
    """Image illisible ou inexploitable, avec un message pour l'utilisateur."""

    default_message_fr = "Je n'arrive pas à lire ces photos 😕"

    def __init__(self, message: str = "", *, user_message_fr: str | None = None) -> None:
        super().__init__(message or self.default_message_fr)
        self.user_message_fr = user_message_fr or self.default_message_fr


def encode_image(img: Image.Image, *, label: str = "", quality: int = JPEG_QUALITY) -> ImagePart:
    """Encode une image PIL en JPEG base64, prete pour l'API."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=quality, optimize=True)
    return ImagePart(
        data_b64=base64.b64encode(buffer.getvalue()).decode("ascii"),
        media_type="image/jpeg",
        label=label,
    )


def prep_slide(path: Path, *, label: str = "", max_side: int = SLIDE_MAX_SIDE) -> ImagePart:
    """Charge une slide TikTok et la reduit pour l'appel d'analyse."""
    img = _load(path)
    img.thumbnail((max_side, max_side))
    return encode_image(img, label=label)


def prep_thumbnail(
    data: bytes, *, label: str = "", max_side: int = THUMBNAIL_MAX_SIDE
) -> ImagePart:
    """Prepare la vignette d'une annonce Vinted pour le re-classement."""
    img = _load(data)
    img.thumbnail((max_side, max_side))
    return encode_image(img, label=label)


def crop_garment(
    slide_path: Path,
    bbox: BBox | None,
    *,
    label: str = "",
    pad: float = 0.12,
    max_side: int = CROP_MAX_SIDE,
) -> ImagePart:
    """Isole le vetement dans la slide, ou renvoie la slide entiere si la bbox ne vaut rien."""
    img = _load(slide_path)
    box = _crop_box(img.size, bbox, pad)
    if box is None:
        log.info("bbox inexploitable pour %s : la slide entiere est envoyee", slide_path.name)
    else:
        img = img.crop(box)
    img.thumbnail((max_side, max_side))
    return encode_image(img, label=label)


async def analyze_slides(
    slide_paths: list[Path], backend: LLMBackend, model: str
) -> AnalysisResult:
    """Identifie les vetements presents sur les slides.

    Laisse remonter LLMError ; toute reponse mal formee est rattrapee plutot
    que fatale, quitte a ne garder qu'une partie des vetements.
    """
    images: list[ImagePart] = []
    # Position envoyee au modele -> index 1-based dans slide_paths.
    origins: list[int] = []
    for index, path in enumerate(slide_paths, start=1):
        try:
            part = await asyncio.to_thread(prep_slide, path, label=f"Image {len(images) + 1}:")
        except VisionError:
            log.warning("slide illisible, ignoree : %s", path)
            continue
        images.append(part)
        origins.append(index)

    if not images:
        raise VisionError(f"aucune slide exploitable parmi {len(slide_paths)}")

    payload = await backend.analyze_slides(
        images,
        system_prompt=ANALYSIS_SYSTEM,
        user_text=analysis_user_text(len(images)),
        model=model,
    )
    garments = _parse_garments(payload, origins)
    log.info("analyse : %d vetement(s) sur %d image(s)", len(garments), len(images))
    return AnalysisResult.model_validate({"garments": garments})


def _load(source: Path | bytes) -> Image.Image:
    """Ouvre une image, corrige son orientation EXIF et la detache du fichier."""
    handle: Any = io.BytesIO(source) if isinstance(source, bytes) else source
    try:
        with Image.open(handle) as raw:
            # exif_transpose renvoie toujours une copie chargee : sortir du
            # `with` ne coupe pas l'image sous nos pieds.
            return ImageOps.exif_transpose(raw).convert("RGB")
    except Exception as exc:
        origin = "vignette" if isinstance(source, bytes) else str(source)
        raise VisionError(f"image illisible : {origin}") from exc


def _crop_box(
    size: tuple[int, int], bbox: BBox | None, pad: float
) -> tuple[int, int, int, int] | None:
    if bbox is None or not bbox.is_usable():
        return None
    width, height = size
    left = round((bbox.x - bbox.w * pad) * width)
    top = round((bbox.y - bbox.h * pad) * height)
    right = round((bbox.x + bbox.w * (1.0 + pad)) * width)
    bottom = round((bbox.y + bbox.h * (1.0 + pad)) * height)

    left = max(0, min(left, width))
    top = max(0, min(top, height))
    right = max(0, min(right, width))
    bottom = max(0, min(bottom, height))
    if right - left < MIN_CROP_PX or bottom - top < MIN_CROP_PX:
        return None
    return left, top, right, bottom


def _parse_garments(payload: Any, origins: list[int]) -> list[Garment]:
    entries = _garment_entries(payload)
    if not entries:
        log.warning("analyse : aucune entree exploitable dans la reponse du modele")
        return []

    garments: list[Garment] = []
    used_ids: set[str] = set()
    for position, entry in enumerate(entries, start=1):
        garment = _validate_garment(_normalize(entry, position, origins))
        if garment is None:
            log.warning("vetement ignore (reponse invalide) : %.200r", entry)
            continue
        if garment.id in used_ids:
            garment = garment.model_copy(update={"id": _free_id(position, used_ids)})
        used_ids.add(garment.id)
        garments.append(garment)
    return garments


def _free_id(position: int, used: set[str]) -> str:
    """Les ids servent de cle a l'appelant : deux vetements n'en partagent jamais un."""
    candidate = f"g{position}"
    suffix = 2
    while candidate in used:
        candidate = f"g{position}-{suffix}"
        suffix += 1
    return candidate


def _garment_entries(payload: Any) -> list[dict[str, Any]]:
    """Extrait la liste des vetements, meme si le modele l'a emballee autrement."""
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    if not isinstance(payload, dict):
        return []
    for key in _LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return [entry for entry in value if isinstance(entry, dict)]
    for value in payload.values():
        if isinstance(value, list) and any(isinstance(entry, dict) for entry in value):
            return [entry for entry in value if isinstance(entry, dict)]
    if any(key in payload for key in _ENTRY_KEYS):
        return [payload]
    return []


def _normalize(entry: dict[str, Any], position: int, origins: list[int]) -> dict[str, Any]:
    data = dict(entry)

    raw_id = data.get("id")
    data["id"] = str(raw_id).strip() if isinstance(raw_id, (str, int)) else ""
    if not data["id"]:
        data["id"] = f"g{position}"

    label = data.get("label_fr") or data.get("label") or data.get("nom") or data.get("name")
    if isinstance(label, (str, int)) and str(label).strip():
        data["label_fr"] = str(label).strip()
    else:
        # Sans libelle il n'y a rien a chercher : l'entree sera rejetee.
        data.pop("label_fr", None)

    queries = data.get("queries_fr")
    if queries is None:
        queries = data.get("queries")
    if isinstance(queries, str):
        queries = [queries]
    data["queries_fr"] = (
        [str(q) for q in queries if isinstance(q, (str, int, float))]
        if isinstance(queries, (list, tuple))
        else []
    )

    data["catalog_id"] = _as_int(data.get("catalog_id"))
    data["color_ids"] = _as_ints(data.get("color_ids"))

    # Un brand_id invente est ecarte ici et pas seulement par models.Garment :
    # sinon il masque le nom de marque, qui lui est peut-etre dans la table.
    brand_id = _as_int(data.get("brand_id"))
    data["brand_id"] = brand_id if brand_id in BRANDS else None

    brand = data.get("brand")
    data["brand"] = brand.strip() or None if isinstance(brand, str) else None
    if data["brand"] and data["brand_id"] is None:
        # Le filtre marque de Vinted marche par id : sans correspondance connue
        # la marque ne sert que dans le texte de recherche.
        data["brand_id"] = BRAND_IDS_BY_NAME.get(data["brand"].lower())

    slide_index = _as_int(data.get("slide_index")) or 1
    data["slide_index"] = origins[min(max(slide_index, 1), len(origins)) - 1]

    bbox = data.get("bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        bbox = dict(zip("xywh", bbox))
    data["bbox"] = bbox if isinstance(bbox, dict) else None

    confidence = data.get("confidence")
    confidence = confidence.strip().lower() if isinstance(confidence, str) else ""
    data["confidence"] = confidence if confidence in _CONFIDENCES else "medium"
    return data


def _validate_garment(data: dict[str, Any]) -> Garment | None:
    try:
        return Garment.model_validate(data)
    except ValidationError:
        pass
    if data.get("bbox") is None:
        return None
    # Une bbox mal formee ne doit pas couter le vetement entier : sans elle on
    # cherchera juste sur la slide complete.
    try:
        return Garment.model_validate({**data, "bbox": None})
    except ValidationError:
        return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_ints(value: Any) -> list[int]:
    if isinstance(value, (int, str)):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [number for number in (_as_int(entry) for entry in value) if number is not None]
