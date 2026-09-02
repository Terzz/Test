"""Extraction TikTok : detection des liens, diaporama vs video, telechargement."""

from __future__ import annotations

import httpx
import pytest

from fripe import tiktok
from fripe.models import SlidePost
from fripe.tiktok import ExtractorDown, NotATikTokUrl, VideoPost, find_tiktok_url
from tests.conftest import make_jpeg


@pytest.fixture
def fake_http(monkeypatch):
    """Remplace httpx.AsyncClient par un transport simule, route par bout d'URL."""
    routes: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        for fragment, response in routes.items():
            if fragment in str(request.url):
                if isinstance(response, dict):
                    return httpx.Response(200, json=response)
                if isinstance(response, bytes):
                    return httpx.Response(200, content=response)
                if isinstance(response, int):
                    return httpx.Response(response)
        return httpx.Response(404)

    real_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(tiktok.httpx, "AsyncClient", factory)

    # curl_cffi et le depliage d'URL sortiraient du transport simule : on les
    # ecarte pour que les tests exercent le chemin httpx mocke.
    async def pas_de_curl_cffi(url):
        raise RuntimeError("curl_cffi indisponible en test")

    async def pas_de_depliage(url):
        return None

    monkeypatch.setattr(tiktok, "_tikwm_via_curl_cffi", pas_de_curl_cffi)
    monkeypatch.setattr(tiktok, "_resolve_short_url", pas_de_depliage)
    return routes


@pytest.mark.parametrize(
    "texte",
    [
        "https://vm.tiktok.com/ZM66UoB9m/",
        "regarde ça https://www.tiktok.com/@unefille/photo/7369836402751671585 stylé",
        "https://www.tiktok.com/t/ZTdabc123/",
        "https://vt.tiktok.com/ZSabc/",
    ],
)
def test_find_tiktok_url_reconnait_les_formats(texte):
    assert find_tiktok_url(texte) is not None


def test_find_tiktok_url_ignore_le_reste():
    assert find_tiktok_url("https://www.vinted.fr/catalog?search_text=veste") is None
    assert find_tiktok_url("coucou") is None
    assert find_tiktok_url("") is None


def test_find_tiktok_url_retire_la_ponctuation_finale():
    url = find_tiktok_url("tiens : https://vm.tiktok.com/ZM66UoB9m/.")
    assert url == "https://vm.tiktok.com/ZM66UoB9m/"


async def test_fetch_slides_diaporama(fake_http, tikwm_photo):
    fake_http["tikwm.com"] = tikwm_photo
    post = await tiktok.fetch_slides("https://vm.tiktok.com/ZM66UoB9m/")

    assert post.post_id == "7369836402751671585"
    assert len(post.image_urls) == 3
    assert post.title.startswith("mes pieces")


async def test_fetch_slides_refuse_une_video(fake_http, tikwm_video):
    fake_http["tikwm.com"] = tikwm_video
    with pytest.raises(VideoPost) as exc:
        await tiktok.fetch_slides("https://www.tiktok.com/@quelquun/video/7369836402751671999")
    assert "diaporama" in exc.value.user_message_fr


async def test_fetch_slides_sans_lien_tiktok(fake_http):
    with pytest.raises(NotATikTokUrl):
        await tiktok.fetch_slides("https://www.vinted.fr/")


async def test_fetch_slides_extracteurs_hs(fake_http):
    # tikwm en erreur applicative, puis gallery-dl indisponible (extra absent).
    fake_http["tikwm.com"] = {"code": -1, "msg": "url parsing is error"}
    with pytest.raises(ExtractorDown) as exc:
        await tiktok.fetch_slides("https://vm.tiktok.com/ZM66UoB9m/")
    assert "Réessaie" in exc.value.user_message_fr


async def test_download_slides_ignore_les_echecs_isoles(fake_http, tmp_path):
    fake_http["slide-1"] = make_jpeg()
    fake_http["slide-2"] = 500
    fake_http["slide-3"] = make_jpeg()

    post = SlidePost(
        post_id="42",
        image_urls=[
            "https://cdn.example/slide-1.jpeg",
            "https://cdn.example/slide-2.jpeg",
            "https://cdn.example/slide-3.jpeg",
        ],
    )
    paths = await tiktok.download_slides(post, tmp_path / "42")

    assert [p.name for p in paths] == ["01.jpg", "03.jpg"]
    assert all(p.exists() and p.stat().st_size > 0 for p in paths)


async def test_download_slides_convertit_le_png_en_jpeg(fake_http, tmp_path):
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (120, 160), "teal").save(buffer, format="PNG")
    fake_http["slide-1"] = buffer.getvalue()

    post = SlidePost(post_id="43", image_urls=["https://cdn.example/slide-1.png"])
    paths = await tiktok.download_slides(post, tmp_path / "43")

    with Image.open(paths[0]) as image:
        assert image.format == "JPEG"


async def test_download_slides_tout_en_echec(fake_http, tmp_path):
    fake_http["slide-"] = 404
    post = SlidePost(post_id="44", image_urls=["https://cdn.example/slide-1.jpeg"])
    with pytest.raises(ExtractorDown):
        await tiktok.download_slides(post, tmp_path / "44")


async def test_fetch_slides_prefere_curl_cffi(monkeypatch, tikwm_photo):
    """curl_cffi passe en premier : c'est lui qui contourne les 403 anti-bot."""
    appels: list[str] = []

    async def faux_curl_cffi(url):
        appels.append(url)
        return tikwm_photo

    async def httpx_interdit(url):
        raise AssertionError("httpx ne doit pas etre appele si curl_cffi repond")

    monkeypatch.setattr(tiktok, "_tikwm_via_curl_cffi", faux_curl_cffi)
    monkeypatch.setattr(tiktok, "_tikwm_via_httpx", httpx_interdit)

    post = await tiktok.fetch_slides("https://vm.tiktok.com/ZGdxGpLHD/")

    assert appels == ["https://vm.tiktok.com/ZGdxGpLHD/"]
    assert len(post.image_urls) == len(tikwm_photo["data"]["images"])


async def test_fetch_slides_retente_avec_l_url_canonique(monkeypatch, tikwm_photo):
    """Un lien court refuse peut passer sous sa forme /@auteur/photo/123."""
    vus: list[str] = []

    async def curl_cffi_selectif(url):
        vus.append(url)
        if url.startswith("https://vm."):
            raise ValueError("HTTP 403")
        return tikwm_photo

    async def httpx_ko(url):
        raise ValueError("HTTP 403")

    async def depliage(url):
        return "https://www.tiktok.com/@qui/photo/7369836402751671585"

    monkeypatch.setattr(tiktok, "_tikwm_via_curl_cffi", curl_cffi_selectif)
    monkeypatch.setattr(tiktok, "_tikwm_via_httpx", httpx_ko)
    monkeypatch.setattr(tiktok, "_resolve_short_url", depliage)

    post = await tiktok.fetch_slides("https://vm.tiktok.com/ZGdxGpLHD/")

    assert vus[-1] == "https://www.tiktok.com/@qui/photo/7369836402751671585"
    assert post.image_urls


async def test_fetch_slides_annonce_une_video_quand_le_lien_court_en_cache_une(monkeypatch):
    """Le lien court ne dit rien : deplie, il revele /video/ et non /photo/."""
    async def tikwm_ko(url):
        raise ValueError("HTTP 403")

    async def depliage(url):
        return "https://www.tiktok.com/@qui/video/7369836402751671585"

    monkeypatch.setattr(tiktok, "_tikwm_via_curl_cffi", tikwm_ko)
    monkeypatch.setattr(tiktok, "_tikwm_via_httpx", tikwm_ko)
    monkeypatch.setattr(tiktok, "_resolve_short_url", depliage)

    with pytest.raises(tiktok.VideoPost) as exc:
        await tiktok.fetch_slides("https://vm.tiktok.com/ZGdxGpLHD/")
    assert "diaporama" in exc.value.user_message_fr


@pytest.mark.parametrize(
    "texte",
    [
        "https://lite.tiktok.com/t/ZS8abc123/",
        "https://www.tiktok.com/photo/7300000000000000000",
        "https://m.tiktok.com/t/ZTdabc123/",
    ],
)
def test_find_tiktok_url_reconnait_tiktok_lite_et_les_liens_sans_auteur(texte):
    assert find_tiktok_url(texte) == texte


def test_find_tiktok_urls_rend_tous_les_liens_sans_doublon():
    from fripe.tiktok import find_tiktok_urls

    texte = (
        "https://vm.tiktok.com/AAA/ et https://vm.tiktok.com/BBB/, "
        "encore https://vm.tiktok.com/AAA/"
    )
    assert find_tiktok_urls(texte) == ["https://vm.tiktok.com/AAA/", "https://vm.tiktok.com/BBB/"]
    assert find_tiktok_urls("rien") == []


def test_clean_share_url_retire_les_parametres_de_suivi():
    from fripe.tiktok import clean_share_url

    sale = "https://www.tiktok.com/@x/photo/123?_t=ZN-8abc&_r=1#ancre"
    assert clean_share_url(sale) == "https://www.tiktok.com/@x/photo/123"
    assert clean_share_url("https://vm.tiktok.com/ZMabc/") == "https://vm.tiktok.com/ZMabc/"


async def test_fetch_slides_transmet_le_lien_nettoye(monkeypatch, tikwm_photo):
    recus = []

    async def faux_tikwm(url):
        recus.append(url)
        return tikwm_photo

    monkeypatch.setattr(tiktok, "_tikwm_via_curl_cffi", faux_tikwm)
    await tiktok.fetch_slides("https://vm.tiktok.com/ZMabc/?_t=ZN-suivi&_r=1")
    assert recus == ["https://vm.tiktok.com/ZMabc/"]


async def test_fetch_slides_ignore_les_images_locales_d_un_tiers(monkeypatch, tikwm_photo):
    payload = dict(tikwm_photo)
    payload["data"] = dict(tikwm_photo["data"])
    payload["data"]["images"] = ["file:///etc/passwd", "https://cdn.exemple/1.jpg"]

    async def faux_tikwm(url):
        return payload

    monkeypatch.setattr(tiktok, "_tikwm_via_curl_cffi", faux_tikwm)
    post = await tiktok.fetch_slides("https://vm.tiktok.com/ZMabc/")
    assert post.image_urls == ["https://cdn.exemple/1.jpg"]


async def test_download_slides_refuse_un_fichier_local_hors_des_dossiers_du_bot(fake_http, tmp_path):
    image = tmp_path / "prive.jpg"
    image.write_bytes(make_jpeg())
    post = SlidePost(post_id="1", image_urls=[image.as_uri()])

    with pytest.raises(ExtractorDown):
        await tiktok.download_slides(post, tmp_path / "dest")


async def test_download_slides_accepte_les_fichiers_du_repli_gallery_dl(fake_http, tmp_path):
    racine = tmp_path / "fripe-gdl-test"
    racine.mkdir()
    image = racine / "1.jpg"
    image.write_bytes(make_jpeg())
    tiktok._TEMP_DIRS.add(racine)
    post = SlidePost(post_id="1", image_urls=[image.as_uri()])

    chemins = await tiktok.download_slides(post, tmp_path / "dest")

    assert [p.name for p in chemins] == ["01.jpg"]
    # download_slides a libere le dossier temporaire au passage.
    assert racine not in tiktok._TEMP_DIRS


def test_le_message_video_est_accentue_et_guide():
    message = VideoPost().user_message_fr
    assert "vidéo" in message and "Renvoie" in message
