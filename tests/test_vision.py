"""Preparation des images et tolerance de l'analyse aux sorties approximatives."""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from fripe.models import BBox
from fripe.vision import VisionError, analyze_slides, crop_garment, prep_slide, prep_thumbnail
from tests.conftest import make_jpeg


def decode(part) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(part.data_b64)))


@pytest.fixture
def slide(tmp_path):
    path = tmp_path / "01.jpg"
    path.write_bytes(make_jpeg(1600, 2000))
    return path


def test_prep_slide_reduit_les_grandes_images(slide):
    part = prep_slide(slide, label="Image 1:")
    image = decode(part)

    assert max(image.size) == 1024
    assert part.label == "Image 1:"
    assert part.media_type == "image/jpeg"


def test_prep_slide_n_agrandit_pas_les_petites(tmp_path):
    path = tmp_path / "petite.jpg"
    path.write_bytes(make_jpeg(300, 200))
    assert decode(prep_slide(path)).size == (300, 200)


def test_prep_thumbnail_plafonne_a_336px():
    assert max(decode(prep_thumbnail(make_jpeg(800, 1200))).size) == 336


def test_crop_garment_decoupe_la_zone_indiquee(slide):
    entier = decode(crop_garment(slide, None)).size
    recadre = decode(crop_garment(slide, BBox(x=0.3, y=0.15, w=0.4, h=0.3))).size

    assert recadre != entier
    # La slide est en portrait (0.8), la zone demandee est plutot large :
    # le rapport largeur/hauteur doit avoir change dans ce sens.
    assert recadre[0] / recadre[1] > entier[0] / entier[1]


@pytest.mark.parametrize(
    "bbox",
    [None, BBox(x=0.3, y=0.3, w=0.001, h=0.5), BBox(x=8.0, y=8.0, w=0.5, h=0.5)],
)
def test_crop_garment_retombe_sur_la_slide_entiere(slide, bbox):
    part = crop_garment(slide, bbox)
    assert decode(part).size == decode(prep_slide(slide, max_side=512)).size


class FakeBackend:
    """Renvoie une charge utile figee et note ce qu'on lui a envoye."""

    def __init__(self, payload) -> None:
        self.payload = payload
        self.images = None

    async def analyze_slides(self, images, *, system_prompt, user_text, model):
        self.images = images
        return self.payload

    async def rerank(self, source, thumbnails, *, system_prompt, user_text, model):
        return self.payload


async def test_analyze_slides_etiquette_les_images(slide):
    backend = FakeBackend({"garments": [{"id": "g1", "label_fr": "veste", "queries_fr": ["veste"]}]})
    result = await analyze_slides([slide, slide], backend, "claude-opus-5")

    assert [part.label for part in backend.images] == ["Image 1:", "Image 2:"]
    assert result.garments[0].label_fr == "veste"


async def test_analyze_slides_ignore_une_entree_invalide(slide):
    backend = FakeBackend(
        {
            "garments": [
                {"id": "g1", "label_fr": "veste en cuir", "queries_fr": ["veste cuir"]},
                {"pas_du_tout": "un vetement"},
                {"id": "g3", "label_fr": "jean", "queries_fr": ["jean bleu"]},
            ]
        }
    )
    result = await analyze_slides([slide], backend, "claude-opus-5")

    # L'entree bancale disparait, les deux autres survivent.
    assert [g.label_fr for g in result.garments] == ["veste en cuir", "jean"]


async def test_analyze_slides_donne_des_identifiants_uniques(slide):
    backend = FakeBackend(
        {
            "garments": [
                {"id": "g2", "label_fr": "veste", "queries_fr": ["veste"]},
                {"id": "g2", "label_fr": "jean", "queries_fr": ["jean"]},
            ]
        }
    )
    result = await analyze_slides([slide], backend, "claude-opus-5")

    identifiants = [g.id for g in result.garments]
    assert len(set(identifiants)) == len(identifiants)


async def test_analyze_slides_borne_le_numero_de_slide(slide):
    backend = FakeBackend(
        {"garments": [{"id": "g1", "label_fr": "veste", "queries_fr": ["veste"], "slide_index": 99}]}
    )
    result = await analyze_slides([slide, slide], backend, "claude-opus-5")
    assert 1 <= result.garments[0].slide_index <= 2


async def test_analyze_slides_refuse_des_photos_illisibles(tmp_path):
    cassee = tmp_path / "cassee.jpg"
    cassee.write_bytes(b"ceci n'est pas une image")

    with pytest.raises(VisionError) as exc:
        await analyze_slides([cassee], FakeBackend({"garments": []}), "claude-opus-5")
    assert exc.value.user_message_fr
