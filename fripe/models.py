"""Modeles de donnees partages par tous les modules."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from fripe.catalog import BRANDS, CATALOGS, COLORS


class ImagePart(BaseModel):
    """Une image prete a etre envoyee au modele (deja encodee en base64)."""

    data_b64: str
    media_type: str = "image/jpeg"
    label: str = ""


class SlidePost(BaseModel):
    """Un post TikTok en mode photo."""

    post_id: str
    title: str = ""
    image_urls: list[str]


class BBox(BaseModel):
    """Zone du vetement dans la slide, en coordonnees normalisees (0-1)."""

    x: float
    y: float
    w: float
    h: float

    def is_usable(self) -> bool:
        """Une bbox trop petite ou hors cadre n'est pas exploitable pour un crop."""
        if self.w < 0.05 or self.h < 0.05:
            return False
        if self.w > 1.5 or self.h > 1.5:
            return False
        return -0.5 < self.x < 1.5 and -0.5 < self.y < 1.5


class Garment(BaseModel):
    """Un vetement identifie sur les slides, avec ses requetes 'langage vendeur'."""

    id: str
    label_fr: str
    queries_fr: list[str] = Field(default_factory=list)
    catalog_id: int | None = None
    color_ids: list[int] = Field(default_factory=list)
    brand: str | None = None
    brand_id: int | None = None
    slide_index: int = 1
    bbox: BBox | None = None
    confidence: Literal["high", "medium", "low"] = "medium"

    @field_validator("catalog_id")
    @classmethod
    def _known_catalog(cls, v: int | None) -> int | None:
        # Un identifiant invente par le modele filtrerait sur une categorie
        # arbitraire : mieux vaut chercher sans filtre de categorie.
        return v if v in CATALOGS else None

    @field_validator("color_ids")
    @classmethod
    def _known_colors(cls, v: list[int]) -> list[int]:
        return [c for c in v if c in COLORS]

    @field_validator("brand_id")
    @classmethod
    def _known_brand(cls, v: int | None) -> int | None:
        return v if v in BRANDS else None

    @field_validator("queries_fr")
    @classmethod
    def _clean_queries(cls, v: list[str]) -> list[str]:
        seen: list[str] = []
        for q in v:
            q = " ".join(str(q).split())
            if q and q.lower() not in {s.lower() for s in seen}:
                seen.append(q)
        return seen[:3]


class AnalysisResult(BaseModel):
    garments: list[Garment] = Field(default_factory=list)


class RerankResult(BaseModel):
    """Sortie d'un lot de re-classement (indices d'images, 1-based)."""

    ranking: list[int] = Field(default_factory=list)
    exact_match_candidates: list[int] = Field(default_factory=list)
    note_fr: str = ""


class VintedItem(BaseModel):
    id: int
    title: str
    url: str
    price_amount: float | None = None
    currency: str = "EUR"
    brand_title: str | None = None
    size_title: str | None = None
    status: str | None = None
    photo_url: str | None = None
    photo_full_url: str | None = None
    seller: str | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "VintedItem":
        """Construit un item depuis la reponse de /api/v2/catalog/items.

        Tolerant : Vinted ajoute et retire des champs sans preavis.
        """
        price = raw.get("price") or {}
        photo = raw.get("photo") or {}
        user = raw.get("user") or {}

        amount = price.get("amount")
        try:
            price_amount = float(amount) if amount is not None else None
        except (TypeError, ValueError):
            price_amount = None

        return cls(
            id=int(raw["id"]),
            title=str(raw.get("title") or "").strip() or "Sans titre",
            url=str(raw.get("url") or ""),
            price_amount=price_amount,
            currency=str(price.get("currency_code") or "EUR"),
            brand_title=raw.get("brand_title") or None,
            size_title=raw.get("size_title") or None,
            status=raw.get("status") or None,
            photo_url=_pick_thumbnail(photo),
            photo_full_url=photo.get("full_size_url") or photo.get("url") or None,
            seller=user.get("login") or None,
        )

    def price_label(self) -> str:
        if self.price_amount is None:
            return "prix inconnu"
        symbol = "€" if self.currency == "EUR" else f" {self.currency}"
        amount = f"{self.price_amount:.2f}".rstrip("0").rstrip(".")
        return f"{amount}{symbol}" if symbol == "€" else f"{amount}{symbol}"


def _pick_thumbnail(photo: dict[str, Any], target: int = 310) -> str | None:
    """Choisit la vignette la plus proche de `target` px, sinon la photo pleine."""
    thumbs = photo.get("thumbnails") or []
    best_url: str | None = None
    best_delta: float | None = None
    for thumb in thumbs:
        url = thumb.get("url")
        width = thumb.get("width")
        if not url or not isinstance(width, (int, float)):
            continue
        delta = abs(float(width) - target)
        if best_delta is None or delta < best_delta:
            best_url, best_delta = url, delta
    return best_url or photo.get("url") or None


class GarmentResult(BaseModel):
    """Resultat final pour un vetement : les annonces retenues, deja classees."""

    garment: Garment
    items: list[VintedItem] = Field(default_factory=list)
    note_fr: str | None = None
