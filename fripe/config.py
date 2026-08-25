"""Chargement et validation de la configuration (.env)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(Exception):
    """Configuration absente ou incoherente : on s'arrete tot avec un message clair."""


@dataclass(frozen=True)
class Config:
    telegram_token: str
    llm_backend: str
    claude_oauth_token: str | None
    anthropic_api_key: str | None
    analysis_model: str
    rerank_model: str
    reranker: str
    data_dir: Path
    max_results: int
    price_to: int | None
    allowed_chat_ids: frozenset[int] = field(default_factory=frozenset)

    def is_allowed(self, chat_id: int) -> bool:
        return not self.allowed_chat_ids or chat_id in self.allowed_chat_ids


def _int_or_none(raw: str | None, name: str) -> int | None:
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} doit etre un nombre entier (recu : {raw!r})") from exc


def load_config(require_telegram: bool = True) -> Config:
    """Lit .env et l'environnement.

    `require_telegram=False` sert aux commandes CLI qui ne parlent pas a Telegram.
    """
    load_dotenv()

    llm_backend = (os.getenv("LLM_BACKEND") or "agent_sdk").strip()
    if llm_backend not in {"agent_sdk", "anthropic_api"}:
        raise ConfigError(
            f"LLM_BACKEND doit valoir 'agent_sdk' ou 'anthropic_api' (recu : {llm_backend!r})"
        )

    oauth_token = (os.getenv("CLAUDE_CODE_OAUTH_TOKEN") or "").strip() or None
    api_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip() or None

    if llm_backend == "agent_sdk":
        if not oauth_token:
            raise ConfigError(
                "CLAUDE_CODE_OAUTH_TOKEN est vide. Lance `claude setup-token` puis "
                "colle le jeton dans le fichier .env."
            )
        # ANTHROPIC_API_KEY a la priorite sur le jeton OAuth dans le SDK, et
        # ClaudeAgentOptions.env fusionne avec l'environnement herite au lieu de
        # le remplacer : sans ce retrait, une cle qui traine facturerait des
        # credits API au lieu d'utiliser l'abonnement.
        if api_key:
            logging.getLogger(__name__).warning(
                "ANTHROPIC_API_KEY est definie mais LLM_BACKEND=agent_sdk : "
                "elle est retiree de l'environnement pour utiliser ton abonnement."
            )
            os.environ.pop("ANTHROPIC_API_KEY", None)
            api_key = None
    elif not api_key:
        raise ConfigError(
            "LLM_BACKEND=anthropic_api mais ANTHROPIC_API_KEY est vide."
        )

    telegram_token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if require_telegram and not telegram_token:
        raise ConfigError(
            "TELEGRAM_BOT_TOKEN est vide. Cree un bot avec @BotFather sur Telegram "
            "et colle le jeton dans le fichier .env."
        )

    raw_ids = os.getenv("ALLOWED_CHAT_IDS") or ""
    try:
        allowed = frozenset(int(part) for part in raw_ids.replace(";", ",").split(",") if part.strip())
    except ValueError as exc:
        raise ConfigError(
            f"ALLOWED_CHAT_IDS doit etre une liste d'entiers separes par des virgules "
            f"(recu : {raw_ids!r})"
        ) from exc

    max_results = _int_or_none(os.getenv("MAX_RESULTS_PER_GARMENT"), "MAX_RESULTS_PER_GARMENT")
    if max_results is None:
        max_results = 6
    # Telegram n'accepte qu'entre 2 et 10 medias par album ; un 0 explicite
    # est clampe comme n'importe quelle valeur hors bornes (pas ecrase par
    # le defaut, ce que ferait un `or 6`).
    max_results = max(2, min(10, max_results))

    reranker = (os.getenv("RERANKER") or "claude").strip()
    if reranker not in {"claude", "siglip"}:
        raise ConfigError(f"RERANKER doit valoir 'claude' ou 'siglip' (recu : {reranker!r})")

    data_dir = Path(os.getenv("DATA_DIR") or "./data").expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        telegram_token=telegram_token,
        llm_backend=llm_backend,
        claude_oauth_token=oauth_token,
        anthropic_api_key=api_key,
        analysis_model=(os.getenv("ANALYSIS_MODEL") or "claude-opus-5").strip(),
        rerank_model=(os.getenv("RERANK_MODEL") or "claude-haiku-4-5").strip(),
        reranker=reranker,
        data_dir=data_dir,
        max_results=max_results,
        price_to=_int_or_none(os.getenv("PRICE_TO"), "PRICE_TO"),
        allowed_chat_ids=allowed,
    )


def setup_logging() -> None:
    level = (os.getenv("LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    # Le polling de PTB est tres bavard en DEBUG.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)
