"""Fixtures partagees : chargement des donnees d'exemple et faux transport HTTP."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from PIL import Image

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def tikwm_photo() -> dict:
    return load_fixture("tikwm_photo.json")


@pytest.fixture
def tikwm_video() -> dict:
    return load_fixture("tikwm_video.json")


@pytest.fixture
def vinted_search() -> dict:
    return load_fixture("vinted_search.json")


def make_jpeg(width: int = 600, height: int = 900, color: str = "sienna") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture
def jpeg_bytes() -> bytes:
    return make_jpeg()
