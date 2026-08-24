"""Mise en forme des reponses Telegram."""

from __future__ import annotations

from fripe.bot import CAPTION_LIMIT, build_caption
from fripe.models import Garment, GarmentResult, VintedItem


def make_item(item_id: int, title: str = "Veste en cuir marron vintage") -> VintedItem:
    return VintedItem(
        id=item_id,
        title=title,
        url=f"https://www.vinted.fr/items/{item_id}",
        price_amount=25.0,
        brand_title="Zara",
        size_title="M",
        status="Très bon état",
        photo_url=f"https://images1.vinted.net/{item_id}.jpeg",
    )


def make_result(items, note=None) -> GarmentResult:
    garment = Garment(id="g1", label_fr="veste en cuir marron", queries_fr=["veste cuir marron"])
    return GarmentResult(garment=garment, items=items, note_fr=note)


def test_caption_liste_les_annonces():
    caption = build_caption(make_result([make_item(1), make_item(2)]))

    assert "veste en cuir marron" in caption
    assert '<a href="https://www.vinted.fr/items/1">' in caption
    assert "25€" in caption
    assert "Zara" in caption
    assert "Très bon état" in caption


def test_caption_affiche_la_note_d_elargissement():
    caption = build_caption(make_result([make_item(1)], note="recherche élargie : couleur ignorée"))
    assert "<i>recherche élargie : couleur ignorée</i>" in caption


def test_caption_echappe_le_html():
    mechant = make_item(1, title="Veste <b>choc</b> & compagnie")
    caption = build_caption(make_result([mechant]))

    assert "<b>choc</b>" not in caption
    assert "&lt;b&gt;choc" in caption
    assert "&amp; compagnie" in caption


def test_caption_reste_sous_la_limite_telegram():
    items = [make_item(i, title="Veste en cuir marron vintage taille M" * 2) for i in range(1, 11)]
    caption = build_caption(make_result(items, note="recherche élargie : filtres ignorés"))

    assert len(caption) <= CAPTION_LIMIT


def test_caption_sans_annonce():
    caption = build_caption(make_result([]))
    assert "veste en cuir marron" in caption
    assert "annonce" not in caption
