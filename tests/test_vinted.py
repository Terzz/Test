"""Client Vinted : construction des parametres, parsing, renouvellement des cookies."""

from __future__ import annotations

import time

import pytest

from fripe.vinted import VintedAuthError, VintedClient, VintedError, _build_params, _parse_items


class FakeResponse:
    def __init__(self, status_code: int, payload=None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("pas de JSON")
        return self._payload


class FakeCookies(dict):
    def clear(self) -> None:  # noqa: D401 - meme signature que curl_cffi
        super().clear()


class FakeSession:
    """Session curl_cffi simulee : rejoue une file de reponses et note les appels."""

    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []
        self.cookies = FakeCookies()

    async def get(self, url, params=None, headers=None):
        self.calls.append((url, dict(params or {})))
        if not self.responses:
            return FakeResponse(500)
        response = self.responses.pop(0)
        if callable(response):
            return response(self)
        return response

    async def close(self) -> None:
        return None


def make_client(tmp_path, responses) -> tuple[VintedClient, FakeSession]:
    client = VintedClient(cookie_cache=tmp_path / "cookies.json", min_delay=0.0, max_delay=0.0)
    session = FakeSession(responses)
    client._session = session
    # Cookies deja valides : on teste search(), pas le bootstrap.
    client._ready_at = time.time()
    client._generation = 1
    client._cache_loaded = True
    return client, session


def test_build_params_omet_les_filtres_vides():
    params = _build_params(
        "  veste   cuir  marron ",
        catalog_ids=None,
        color_ids=[],
        brand_ids=None,
        price_to=None,
        per_page=32,
        page=1,
        order="relevance",
    )
    assert params["search_text"] == "veste cuir marron"
    assert "catalog_ids" not in params
    assert "color_ids" not in params
    assert "brand_ids" not in params
    assert "price_to" not in params


def test_build_params_joint_les_identifiants_et_plafonne():
    params = _build_params(
        "veste",
        catalog_ids=[1037, 1908, 1908],
        color_ids=[2, 4],
        brand_ids=[53],
        price_to=40,
        per_page=500,
        page=0,
        order="n_importe_quoi",
    )
    assert params["catalog_ids"] == "1037,1908"
    assert params["color_ids"] == "2,4"
    assert params["brand_ids"] == "53"
    assert params["price_to"] == "40"
    # Vinted plafonne per_page a 96 et la page commence a 1.
    assert params["per_page"] == "96"
    assert params["page"] == "1"
    assert params["order"] == "relevance"


def test_parse_items_ignore_les_annonces_bancales(vinted_search):
    items = _parse_items(vinted_search)
    # La 4e entree du jeu d'essai n'a ni id ni titre : elle est ecartee.
    assert [item.id for item in items] == [9765188206, 9765188207, 9765188208]


def test_parse_items_exige_le_champ_items():
    with pytest.raises(VintedError):
        _parse_items({"pagination": {}})


async def test_search_renvoie_les_annonces(tmp_path, vinted_search):
    client, session = make_client(tmp_path, [FakeResponse(200, vinted_search)])
    items = await client.search("veste cuir marron", catalog_ids=[1908], color_ids=[2])

    assert len(items) == 3
    url, params = session.calls[0]
    assert url.endswith("/api/v2/catalog/items")
    assert params["search_text"] == "veste cuir marron"
    assert params["catalog_ids"] == "1908"


async def test_search_zero_resultat_n_est_pas_une_erreur(tmp_path):
    client, _ = make_client(tmp_path, [FakeResponse(200, {"items": [], "pagination": {}})])
    assert await client.search("chapeau en gruyère") == []


async def test_search_renouvelle_les_cookies_sur_401(tmp_path, vinted_search):
    def bootstrap(session: FakeSession) -> FakeResponse:
        session.cookies["access_token_web"] = "jeton-neuf"
        return FakeResponse(200)

    client, session = make_client(
        tmp_path,
        [FakeResponse(401), bootstrap, FakeResponse(200, vinted_search)],
    )
    items = await client.search("veste")

    assert len(items) == 3
    # Appel refuse, passage par la page d'accueil, puis nouvel appel a l'API.
    assert [url for url, _ in session.calls] == [
        "https://www.vinted.fr/api/v2/catalog/items",
        "https://www.vinted.fr/",
        "https://www.vinted.fr/api/v2/catalog/items",
    ]
    assert (tmp_path / "cookies.json").exists()


async def test_search_abandonne_apres_un_second_401(tmp_path):
    client, _ = make_client(
        tmp_path,
        [FakeResponse(401), FakeResponse(200), FakeResponse(403)],
    )
    with pytest.raises(VintedAuthError) as exc:
        await client.search("veste")
    assert "Vinted" in exc.value.user_message_fr


async def test_search_signale_la_limitation_de_rythme(tmp_path):
    client, _ = make_client(tmp_path, [FakeResponse(429)])
    with pytest.raises(VintedError) as exc:
        await client.search("veste")
    assert "rythme" in exc.value.user_message_fr
