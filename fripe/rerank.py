"""Re-classement visuel des annonces Vinted face a la photo source."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from fripe.config import Config
from fripe.llm import LLMBackend
from fripe.models import ImagePart, RerankResult, VintedItem
from fripe.prompts import RERANK_SYSTEM, rerank_user_text
from fripe.vision import prep_thumbnail

log = logging.getLogger(__name__)

__all__ = ["ClaudeReranker", "Reranker", "build_reranker"]

# Au-dela d'une vingtaine d'images dans un meme message, le modele confond les
# numeros : on decoupe en lots de 15 vignettes (+ la source).
BATCH_SIZE = 15
_MAX_CONCURRENT_DOWNLOADS = 6
_DOWNLOAD_TIMEOUT_S = 15.0


class Reranker(Protocol):
    """Reordonne des annonces selon leur ressemblance avec la photo source."""

    async def rank(
        self, source: ImagePart, candidates: list[VintedItem], *, garment_label: str = ""
    ) -> list[VintedItem]:
        ...


@dataclass(frozen=True)
class _BatchRanking:
    """Sortie d'un lot : classement propose et correspondances exactes, en annonces."""

    ranking: list[VintedItem]
    exact: list[VintedItem]


class ClaudeReranker:
    """Compare la photo source aux vignettes des annonces, lot par lot."""

    def __init__(
        self,
        backend: LLMBackend,
        model: str,
        http: httpx.AsyncClient,
        *,
        batch_size: int = BATCH_SIZE,
        garment_label: str = "",
    ) -> None:
        self._backend = backend
        self._model = model
        self._http = http
        self._batch_size = max(1, batch_size)
        self._garment_label = garment_label

    async def rank(
        self, source: ImagePart, candidates: list[VintedItem], *, garment_label: str = ""
    ) -> list[VintedItem]:
        # Le libelle arrive par appel, pas par attribut : deux recherches
        # simultanees partagent la meme instance.
        label = garment_label or self._garment_label
        if len(candidates) < 2:
            return list(candidates)

        thumbnails = await self._fetch_thumbnails(candidates)
        kept = [item for item in candidates if item.id in thumbnails]
        dropped = [item for item in candidates if item.id not in thumbnails]
        if len(kept) < 2:
            log.info(
                "re-classement abandonne : %d vignette(s) exploitable(s) sur %d",
                len(kept),
                len(candidates),
            )
            return list(candidates)

        batches = [
            kept[start : start + self._batch_size]
            for start in range(0, len(kept), self._batch_size)
        ]
        # En sequence : chaque lot lance un processus `claude` de plusieurs
        # centaines de Mo ; deux liens simultanes en feraient quatre a la fois.
        results = [await self._rank_batch(source, batch, thumbnails, label) for batch in batches]
        return _merge(results, kept, dropped)

    async def _fetch_thumbnails(
        self, candidates: Sequence[VintedItem]
    ) -> dict[int, ImagePart]:
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_DOWNLOADS)

        async def fetch(item: VintedItem) -> tuple[int, ImagePart] | None:
            if not item.photo_url:
                return None
            async with semaphore:
                try:
                    response = await self._http.get(item.photo_url, timeout=_DOWNLOAD_TIMEOUT_S)
                    response.raise_for_status()
                    part = await asyncio.to_thread(prep_thumbnail, response.content)
                except Exception:
                    log.debug("vignette non recuperee : %s", item.photo_url, exc_info=True)
                    return None
            return item.id, part

        pairs = await asyncio.gather(*(fetch(item) for item in candidates))
        return {pair[0]: pair[1] for pair in pairs if pair is not None}

    async def _rank_batch(
        self,
        source: ImagePart,
        batch: Sequence[VintedItem],
        thumbnails: dict[int, ImagePart],
        garment_label: str = "",
    ) -> _BatchRanking:
        # Le modele raisonne sur des numeros d'image : la source est toujours 1,
        # les annonces du lot suivent a partir de 2.
        images = [
            thumbnails[item.id].model_copy(update={"label": f"Image {position}:"})
            for position, item in enumerate(batch, start=2)
        ]
        try:
            payload = await self._backend.rerank(
                source.model_copy(update={"label": "Image 1:"}),
                images,
                system_prompt=RERANK_SYSTEM,
                user_text=rerank_user_text(garment_label or self._garment_label, len(batch)),
                model=self._model,
            )
            result = RerankResult.model_validate(_coerce_result(payload))
        except Exception as exc:
            # Le re-classement est une couche de confort : un lot rate garde
            # simplement l'ordre de pertinence de Vinted, sans emporter les
            # autres lots. CancelledError derive de BaseException et passe.
            log.warning("lot de re-classement ignore (%s) : %s", type(exc).__name__, exc)
            return _BatchRanking(ranking=list(batch), exact=[])

        if result.note_fr:
            log.debug("note du re-classement : %s", result.note_fr)
        return _BatchRanking(
            ranking=_pick(result.ranking, batch),
            exact=_pick(result.exact_match_candidates, batch),
        )


def build_reranker(cfg: Config, backend: LLMBackend, http: httpx.AsyncClient) -> Reranker:
    """Construit le re-classeur demande par la config, avec repli sur Claude."""
    if cfg.reranker == "siglip":
        # Le module fripe.siglip n'a jamais ete ecrit : mieux vaut le dire que
        # laisser croire qu'un extra manquant est en cause.
        log.warning(
            "RERANKER=siglip n'est pas encore disponible dans cette version : "
            "repli sur le re-classement Claude."
        )
    return ClaudeReranker(backend, cfg.rerank_model, http)


def _pick(numbers: Sequence[int], batch: Sequence[VintedItem]) -> list[VintedItem]:
    """Convertit des numeros d'image (1 = source, 2 = 1re vignette du lot) en annonces."""
    picked: list[VintedItem] = []
    seen: set[int] = set()
    for number in numbers:
        index = number - 2
        if number in seen or index < 0 or index >= len(batch):
            continue
        seen.add(number)
        picked.append(batch[index])
    return picked


def _merge(
    results: Sequence[_BatchRanking],
    kept: Sequence[VintedItem],
    dropped: Sequence[VintedItem],
) -> list[VintedItem]:
    """Correspondances exactes d'abord, classements entrelaces ensuite, ordre Vinted en dernier."""
    merged: list[VintedItem] = []
    seen: set[int] = set()

    def push(items: Sequence[VintedItem]) -> None:
        for item in items:
            if item.id in seen:
                continue
            seen.add(item.id)
            merged.append(item)

    for result in results:
        push(result.exact)
    for rank in range(max((len(result.ranking) for result in results), default=0)):
        for result in results:
            if rank < len(result.ranking):
                push([result.ranking[rank]])
    push(kept)
    push(dropped)
    return merged


def _coerce_result(payload: Any) -> dict[str, Any]:
    """Tolere une liste nue ou des numeros en texte la ou un objet est attendu."""
    if isinstance(payload, (list, tuple)):
        payload = {"ranking": list(payload)}
    if not isinstance(payload, dict):
        # Rendre {} ferait passer le lot pour un classement vide : autant le
        # traiter comme un echec, journalise par l'appelant.
        raise TypeError(f"reponse de re-classement inexploitable : {type(payload).__name__}")
    note = payload.get("note_fr")
    return {
        "ranking": _as_ints(payload.get("ranking")),
        "exact_match_candidates": _as_ints(payload.get("exact_match_candidates")),
        "note_fr": note.strip() if isinstance(note, str) else "",
    }


def _as_ints(value: Any) -> list[int]:
    if isinstance(value, (int, str)):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    numbers: list[int] = []
    for entry in value:
        if isinstance(entry, bool):
            continue
        try:
            numbers.append(int(entry))
        except (TypeError, ValueError):
            continue
    return numbers
