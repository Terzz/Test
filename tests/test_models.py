"""Parsing des annonces Vinted et garde-fous sur les sorties du modele."""

from __future__ import annotations

import pytest

from fripe.models import BBox, Garment, VintedItem


def test_from_api_lit_les_champs_utiles(vinted_search):
    item = VintedItem.from_api(vinted_search["items"][0])

    assert item.id == 9765188206
    assert item.title == "Veste en cuir marron vintage"
    assert item.price_amount == 25.0
    assert item.brand_title == "Zara"
    assert item.size_title == "M / 38 / 10"
    assert item.status == "Très bon état"
    assert item.seller == "sandrine_59"
    # La vignette la plus proche de 310 px, pas la premiere de la liste.
    assert item.photo_url == "https://images1.vinted.net/t/thumb310.jpeg"


def test_from_api_sans_vignette_retombe_sur_la_photo(vinted_search):
    item = VintedItem.from_api(vinted_search["items"][1])
    assert item.photo_url == "https://images1.vinted.net/t/photo2.jpeg"
    assert item.brand_title is None


def test_from_api_tolere_un_prix_absent(vinted_search):
    item = VintedItem.from_api(vinted_search["items"][2])
    assert item.price_amount is None
    assert item.price_label() == "prix inconnu"
    assert item.photo_url is None


def test_from_api_rejette_une_annonce_sans_id(vinted_search):
    with pytest.raises(Exception):
        VintedItem.from_api(vinted_search["items"][3])


@pytest.mark.parametrize(
    ("amount", "attendu"),
    [(25.0, "25€"), (40.50, "40.5€"), (7.0, "7€")],
)
def test_price_label(amount, attendu):
    item = VintedItem(id=1, title="x", url="u", price_amount=amount)
    assert item.price_label() == attendu


def test_garment_ignore_les_identifiants_inventes():
    garment = Garment(
        id="g1",
        label_fr="veste en cuir marron",
        queries_fr=["veste cuir marron"],
        catalog_id=999999,
        color_ids=[2, 12, 4242],
        brand_id=999999,
    )
    assert garment.catalog_id is None
    assert garment.color_ids == [2, 12]
    assert garment.brand_id is None


def test_garment_deduplique_et_plafonne_les_requetes():
    garment = Garment(
        id="g1",
        label_fr="pull",
        queries_fr=["  pull   beige ", "PULL BEIGE", "pull", "pull en laine", "gilet"],
    )
    assert garment.queries_fr == ["pull beige", "pull", "pull en laine"]


def test_bbox_usable():
    assert BBox(x=0.3, y=0.15, w=0.42, h=0.55).is_usable()
    assert not BBox(x=0.3, y=0.3, w=0.01, h=0.5).is_usable()
    assert not BBox(x=9.0, y=0.3, w=0.5, h=0.5).is_usable()
