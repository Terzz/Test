"""Orchestration : fusion des variantes, echelle d'elargissement, chaine complete."""

from __future__ import annotations

from pathlib import Path

import pytest

from fripe import pipeline
from fripe.config import Config
from fripe.models import AnalysisResult, Garment, GarmentResult, ImagePart, VintedItem
from fripe.pipeline import Deps, _merge_round_robin, _search_garment, process_link


def make_item(item_id: int, title: str = "annonce") -> VintedItem:
    return VintedItem(
        id=item_id,
        title=f"{title} {item_id}",
        url=f"https://www.vinted.fr/items/{item_id}",
        price_amount=20.0,
        photo_url=f"https://images1.vinted.net/{item_id}.jpeg",
    )


class FakeVinted:
    """Renvoie des listes programmees et note les filtres recus."""

    def __init__(self, responses: list[list[VintedItem]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def search(self, query, **kwargs):
        self.calls.append({"query": query, **kwargs})
        return self.responses.pop(0) if self.responses else []

    async def close(self) -> None:
        return None


class FakeReranker:
    """Inverse l'ordre : un effet visible et facile a verifier."""

    def __init__(self) -> None:
        self.calls = 0

    async def rank(self, source, candidates, **kwargs):
        self.calls += 1
        return list(reversed(candidates))


def make_config(tmp_path: Path, **overrides) -> Config:
    defaults = dict(
        telegram_token="x",
        llm_backend="agent_sdk",
        claude_oauth_token="t",
        anthropic_api_key=None,
        analysis_model="claude-opus-5",
        rerank_model="claude-haiku-4-5",
        reranker="claude",
        data_dir=tmp_path,
        max_results=6,
        price_to=None,
        allowed_chat_ids=frozenset(),
    )
    defaults.update(overrides)
    return Config(**defaults)


def make_deps(vinted, reranker=None) -> Deps:
    return Deps(vinted=vinted, backend=object(), reranker=reranker or FakeReranker(), http=object())


def test_merge_round_robin_entrelace_et_deduplique():
    a = [make_item(1), make_item(2), make_item(3)]
    b = [make_item(2), make_item(4)]
    merged = _merge_round_robin([a, b])
    assert [item.id for item in merged] == [1, 2, 4, 3]


def test_merge_round_robin_liste_vide():
    assert _merge_round_robin([]) == []
    assert _merge_round_robin([[], []]) == []


async def test_echelle_s_arrete_si_assez_de_resultats(tmp_path):
    vinted = FakeVinted([[make_item(i) for i in range(10)]])
    garment = Garment(id="g1", label_fr="veste", queries_fr=["veste cuir"], color_ids=[2])

    items, note = await _search_garment(garment, make_config(tmp_path), make_deps(vinted))

    assert len(items) == 10
    assert note is None
    assert len(vinted.calls) == 1


async def test_echelle_relache_la_couleur_puis_les_filtres(tmp_path):
    vinted = FakeVinted([[make_item(1)], [make_item(2)], [make_item(3), make_item(4)]])
    garment = Garment(
        id="g1",
        label_fr="veste en cuir marron",
        queries_fr=["veste cuir marron"],
        catalog_id=1908,
        color_ids=[2],
    )

    items, note = await _search_garment(garment, make_config(tmp_path), make_deps(vinted))

    # Les resultats elargis se rangent derriere les precis, jamais entrelaces.
    assert [item.id for item in items] == [1, 2, 3, 4]
    assert note == "recherche élargie : filtres ignorés"
    # 1) filtres complets, 2) sans couleur, 3) texte seul
    assert vinted.calls[0]["color_ids"] == [2]
    assert vinted.calls[1]["color_ids"] is None
    assert vinted.calls[1]["catalog_ids"] == [1908]
    assert vinted.calls[2]["catalog_ids"] is None


async def test_echelle_sans_couleur_a_relacher(tmp_path):
    vinted = FakeVinted([[make_item(1)], [make_item(2)]])
    garment = Garment(id="g1", label_fr="sac noir", queries_fr=["sac"], catalog_id=19)

    _, note = await _search_garment(garment, make_config(tmp_path), make_deps(vinted))

    assert note == "recherche élargie : filtres ignorés"
    assert len(vinted.calls) == 2


@pytest.fixture
def stub_stages(monkeypatch, tmp_path):
    """Court-circuite TikTok et l'analyse visuelle : seul l'enchainement est teste."""
    slide = tmp_path / "slide.jpg"
    slide.write_bytes(b"pas vraiment un jpeg")

    async def fake_fetch(url):
        from fripe.models import SlidePost

        return SlidePost(post_id="42", image_urls=["https://cdn/1.jpg"])

    async def fake_download(post, dest, **kwargs):
        return [slide]

    garments = [
        Garment(id="g1", label_fr="veste en cuir marron", queries_fr=["veste cuir marron"]),
        Garment(id="g2", label_fr="jean délavé", queries_fr=["jean bleu clair"]),
    ]

    async def fake_analyze(paths, backend, model):
        return AnalysisResult(garments=garments)

    def fake_crop(path, bbox, **kwargs):
        return ImagePart(data_b64="", label=kwargs.get("label", ""))

    monkeypatch.setattr(pipeline, "fetch_slides", fake_fetch)
    monkeypatch.setattr(pipeline, "download_slides", fake_download)
    monkeypatch.setattr(pipeline, "analyze_slides", fake_analyze)
    monkeypatch.setattr(pipeline, "crop_garment", fake_crop)
    return garments


async def test_process_link_un_resultat_par_vetement(tmp_path, stub_stages):
    vinted = FakeVinted([[make_item(i) for i in range(1, 11)], [make_item(i) for i in range(20, 30)]])
    reranker = FakeReranker()
    messages: list[str] = []

    async def progress(message):
        messages.append(message)

    results = await process_link(
        "https://vm.tiktok.com/ZM66UoB9m/",
        make_config(tmp_path, max_results=3),
        make_deps(vinted, reranker),
        progress,
    )

    assert [r.garment.id for r in results] == ["g1", "g2"]
    assert all(isinstance(r, GarmentResult) for r in results)
    # max_results respecte, et l'ordre vient bien du re-classement (inverse ici).
    assert [item.id for item in results[0].items] == [10, 9, 8]
    assert reranker.calls == 2
    assert any("photo" in m for m in messages)


async def test_process_link_sans_annonce(tmp_path, stub_stages):
    vinted = FakeVinted([[], [], [], [], [], []])
    results = await process_link(
        "https://vm.tiktok.com/ZM66UoB9m/", make_config(tmp_path), make_deps(vinted)
    )
    assert [r.items for r in results] == [[], []]


async def test_process_link_refuse_un_lien_non_tiktok(tmp_path, stub_stages):
    from fripe.tiktok import NotATikTokUrl

    with pytest.raises(NotATikTokUrl):
        await process_link("https://www.vinted.fr/", make_config(tmp_path), make_deps(FakeVinted([])))


async def test_process_link_survit_a_un_reclassement_casse(tmp_path, stub_stages):
    class BrokenReranker:
        async def rank(self, source, candidates, **kwargs):
            raise RuntimeError("modèle indisponible")

    vinted = FakeVinted([[make_item(i) for i in range(1, 11)], [make_item(i) for i in range(20, 30)]])
    results = await process_link(
        "https://vm.tiktok.com/ZM66UoB9m/",
        make_config(tmp_path, max_results=2),
        make_deps(vinted, BrokenReranker()),
    )
    # Repli sur l'ordre de pertinence de Vinted, sans interrompre la recherche.
    assert [item.id for item in results[0].items] == [1, 2]


def test_slide_for_suit_les_numeros_pas_les_positions(tmp_path):
    from fripe.pipeline import _slide_for

    # La slide 02 n'a pas pu etre telechargee : la liste a un trou.
    paths = [tmp_path / "01.jpg", tmp_path / "03.jpg"]
    assert _slide_for(paths, 1).name == "01.jpg"
    assert _slide_for(paths, 3).name == "03.jpg"
    # Numero absent ou farfelu : on retombe sur la premiere slide disponible.
    assert _slide_for(paths, 2).name == "01.jpg"
    assert _slide_for(paths, 99).name == "01.jpg"
