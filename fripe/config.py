"""Chargement et validation de la configuration (.env)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
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
    # ALLOWED_CHAT_IDS=* : ouvert a tout le monde, en connaissance de cause.
    open_to_all: bool = False

    def is_allowed(self, chat_id: int) -> bool:
        # Ferme par defaut : un inconnu qui trouve le bot consommerait le
        # credit mensuel, et le rattrapage traiterait sa nuit de liens en bloc.
        return self.open_to_all or chat_id in self.allowed_chat_ids


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

    raw_ids = (os.getenv("ALLOWED_CHAT_IDS") or "").strip()
    open_to_all = raw_ids == "*"
    try:
        allowed = frozenset(
            int(part)
            for part in ("" if open_to_all else raw_ids).replace(";", ",").split(",")
            if part.strip()
        )
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

    # Config garde les jetons ; l'environnement, non : tout sous-processus
    # (gallery-dl, CLI claude) en heriterait, et `ps -E` les afficherait. Le
    # backend Agent SDK transmet lui-meme le jeton OAuth a son sous-processus.
    for name in ("TELEGRAM_BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        os.environ.pop(name, None)

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
        open_to_all=open_to_all,
    )


_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"


class _MaskingFormatter(logging.Formatter):
    """Remplace les jetons par *** dans tout ce qui part au journal, traces comprises."""

    def __init__(self, fmt: str | None, datefmt: str | None, secrets: list[str]) -> None:
        super().__init__(fmt, datefmt)
        self._secrets = [s for s in secrets if s and len(s) >= 8]

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text


def mask_secrets(secrets: list[str | None]) -> None:
    """A appeler des que les jetons sont connus : les bibliotheques les citent parfois."""
    kept = [s for s in secrets if s]
    for handler in logging.getLogger().handlers:
        current = handler.formatter
        handler.setFormatter(
            _MaskingFormatter(
                getattr(current, "_fmt", None) or _LOG_FORMAT,
                getattr(current, "datefmt", None),
                kept,
            )
        )


def log_handlers() -> list[logging.Handler] | None:
    """Journal tournant si FRIPE_LOG_FILE est defini (launchd, systemd), sinon la console.

    Un service qui tourne des semaines ne doit pas remplir le disque : trois
    fichiers de 2 Mo couvrent largement un usage perso.
    """
    log_file = (os.getenv("FRIPE_LOG_FILE") or "").strip()
    if not log_file:
        return None
    path = Path(log_file).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    try:
        # Le journal peut citer des messages prives : lisible par ce compte seul.
        os.chmod(path, 0o600)
    except OSError:
        pass
    return [handler]


def setup_logging() -> None:
    # LOG_LEVEL et FRIPE_LOG_FILE peuvent venir du .env ; load_dotenv n'ecrase
    # pas une variable deja posee par launchd ou la ligne de commande.
    load_dotenv()
    level = (os.getenv("LOG_LEVEL") or "INFO").upper()
    handlers = log_handlers()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format=_LOG_FORMAT,
        # Dans un fichier qui traverse les jours, l'heure seule ne suffit pas.
        datefmt="%Y-%m-%d %H:%M:%S" if handlers else "%H:%M:%S",
        handlers=handlers,
        # Sans force, basicConfig se tait si un handler existe deja : le
        # journal fichier deviendrait silencieusement une sortie console.
        force=True,
    )
    # Le polling de PTB est tres bavard en DEBUG.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)
