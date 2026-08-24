"""Re-classement visuel : ordre de fusion et resistance aux pannes."""

from __future__ import annotations

import httpx
import pytest

from fripe.models import ImagePart, VintedItem
from fripe.rerank import ClaudeReranker
from tests.conftest import make_jpeg


def make_item(item_id: int, *, photo: bool = True) -> VintedItem:
    return VintedItem(
        id=item_id,
        title=f"annonce {item_id}",
        url=f"https://www.vinted.fr/items/{item_id}",
        price_amount=20.0,
        photo_url=f"https://images1.vinted.net/{item_id}.jpeg" if photo else None,
    )


@pytest.fixture
def http():
    def handler(request: httpx.Request) -> httpx.Response:
        if "manquante" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, content=make_jpeg(310, 430))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class ScriptedBackend:
    """Rejoue une reponse de re-classement par lot et note les prompts recus."""

    def __init__(self, payloads) -> None:
        self.payloads = list(payloads)
        self.prompts: list[str] = []
        self.batch_sizes: list[int] = []

    async def analyze_slides(self, images, *, system_prompt, user_text, model):
        raise AssertionError("non utilise ici")

    async def rerank(self, source, thumbnails, *, system_prompt, user_text, model):
        self.prompts.append(user_text)
        self.batch_sizes.append(len(thumbnails))
        if not self.payloads:
            return {"ranking": [], "exact_match_candidates": []}
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return payload


SOURCE = ImagePart(data_b64="AAA", label="Image 1:")


async def test_remonte_le_classement_du_modele(http):
    # Image 2 = annonce 1, image 3 = annonce 2, image 4 = annonce 3.
    backend = ScriptedBackend([{"ranking": [4, 2, 3], "exact_match_candidates": []}])
    reranker = ClaudeReranker(backend, "claude-haiku-4-5", http)

    ranked = await reranker.rank(SOURCE, [make_item(1), make_item(2), make_item(3)])

    assert [item.id for item in ranked] == [3, 1, 2]
    await http.aclose()


async def test_les_correspondances_exactes_passent_devant(http):
    backend = ScriptedBackend([{"ranking": [2, 3, 4], "exact_match_candidates": [4]}])
    reranker = ClaudeReranker(backend, "claude-haiku-4-5", http)

    ranked = await reranker.rank(SOURCE, [make_item(1), make_item(2), make_item(3)])

    assert ranked[0].id == 3
    await http.aclose()


async def test_indices_farfelus_ignores(http):
    # 1 designe la source, 99 n'existe pas : seul 3 est exploitable.
    backend = ScriptedBackend([{"ranking": [1, 99, 3, 3], "exact_match_candidates": []}])
    reranker = ClaudeReranker(backend, "claude-haiku-4-5", http)

    ranked = await reranker.rank(SOURCE, [make_item(1), make_item(2)])

    assert [item.id for item in ranked] == [2, 1]
    await http.aclose()


async def test_decoupe_en_lots_et_transmet_le_libelle(http):
    backend = ScriptedBackend(
        [
            {"ranking": [], "exact_match_candidates": []},
            {"ranking": [], "exact_match_candidates": []},
        ]
    )
    reranker = ClaudeReranker(backend, "claude-haiku-4-5", http, batch_size=2)

    await reranker.rank(
        SOURCE, [make_item(i) for i in range(1, 5)], garment_label="veste en cuir marron"
    )

    assert backend.batch_sizes == [2, 2]
    assert all("veste en cuir marron" in prompt for prompt in backend.prompts)
    await http.aclose()


async def test_un_lot_en_echec_garde_l_ordre_de_vinted(http):
    backend = ScriptedBackend([RuntimeError("modèle indisponible")])
    reranker = ClaudeReranker(backend, "claude-haiku-4-5", http)

    items = [make_item(1), make_item(2), make_item(3)]
    ranked = await reranker.rank(SOURCE, items)

    assert [item.id for item in ranked] == [1, 2, 3]
    await http.aclose()


async def test_vignettes_manquantes_reléguées_en_fin(http):
    backend = ScriptedBackend([{"ranking": [2, 3], "exact_match_candidates": []}])
    reranker = ClaudeReranker(backend, "claude-haiku-4-5", http)

    sans_photo = VintedItem(
        id=42, title="annonce manquante", url="https://www.vinted.fr/items/42",
        photo_url="https://images1.vinted.net/manquante.jpeg",
    )
    ranked = await reranker.rank(SOURCE, [make_item(1), sans_photo, make_item(2)])

    assert ranked[-1].id == 42
    await http.aclose()


async def test_pas_d_appel_au_modele_sous_deux_candidats(http):
    backend = ScriptedBackend([])
    reranker = ClaudeReranker(backend, "claude-haiku-4-5", http)

    ranked = await reranker.rank(SOURCE, [make_item(1)])

    assert [item.id for item in ranked] == [1]
    assert backend.batch_sizes == []
    await http.aclose()
