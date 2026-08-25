"""Acces aux modeles Claude : backend Agent SDK (abonnement) ou API Anthropic."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any, Protocol

from fripe.config import Config
from fripe.models import ImagePart

log = logging.getLogger(__name__)

# Les deux paquets sont importes en differe : une installation qui n'utilise que
# l'API Anthropic n'a pas forcement claude-agent-sdk, et inversement.
try:
    from claude_agent_sdk import (  # type: ignore[import-not-found]
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        TextBlock,
    )
except ImportError as exc:  # pragma: no cover - depend de l'installation
    AssistantMessage = None  # type: ignore[assignment,misc]
    ClaudeAgentOptions = None  # type: ignore[assignment,misc]
    ClaudeSDKClient = None  # type: ignore[assignment,misc]
    TextBlock = None  # type: ignore[assignment,misc]
    _AGENT_SDK_ERROR: Exception | None = exc
else:
    _AGENT_SDK_ERROR = None

try:
    from anthropic import APITimeoutError, AsyncAnthropic  # type: ignore[import-not-found]
except ImportError as exc:  # pragma: no cover - depend de l'installation
    AsyncAnthropic = None  # type: ignore[assignment,misc]
    _ANTHROPIC_ERROR: Exception | None = exc
    # `except` n'accepte que des classes : sans le paquet, seul le delai
    # asyncio existe.
    _TIMEOUT_ERRORS: tuple[type[BaseException], ...] = (TimeoutError,)
else:
    _ANTHROPIC_ERROR = None
    # Le client anthropic a son propre delai et leve APITimeoutError, qui
    # n'herite pas de TimeoutError : sans elle l'utilisateur recevrait le
    # message generique au lieu du message d'attente.
    _TIMEOUT_ERRORS = (TimeoutError, APITimeoutError)

_GENERIC_FR = "L'analyse a échoué, réessaie dans un instant."
_TIMEOUT_FR = "L'analyse a mis trop de temps, réessaie."
_PARSE_FR = "L'analyse a renvoyé une réponse inattendue, réessaie."
_REFUSAL_FR = "Le modèle a refusé d'analyser ces images."
_SETUP_FR = "Le service d'analyse est mal configuré sur le serveur."

# Une reponse tronquee est illisible. Le modele d'analyse par defaut (opus 5)
# raisonne en mode adaptatif et ses tokens de reflexion sont decomptes de
# max_tokens : le JSON n'arrive qu'apres. Le plafond de re-classement reste bas
# car la sortie attendue tient en quelques dizaines de tokens (haiku, sans
# reflexion) ; a relever si RERANK_MODEL passe sur un modele qui reflechit.
MAX_TOKENS_ANALYSIS = 16000
MAX_TOKENS_RERANK = 1024

# `allowed_tools` ne fait qu'auto-approuver : seul `disallowed_tools` restreint
# reellement. On coupe tout, ces appels doivent rester hermetiques.
_DISALLOWED_TOOLS = [
    "Bash",
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "Task",
]

_MAX_RAW_CHARS = 2000
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


class LLMError(Exception):
    """Echec d'un appel au modele, avec un message pretable a l'utilisateur."""

    def __init__(self, message: str, *, user_message_fr: str = _GENERIC_FR) -> None:
        super().__init__(message)
        self.user_message_fr = user_message_fr


def _first_balanced_object(text: str) -> str | None:
    """Premier objet {...} equilibre, en ignorant les accolades entre guillemets."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        start = text.find("{", start + 1)
    return None


def _json_candidates(text: str) -> list[str]:
    # La reponse entiere passe en premier : une liste JSON nue serait sinon
    # reduite a son premier objet par le scan d'accolades, sans erreur.
    candidates: list[str] = []
    stripped = text.strip()
    if stripped:
        candidates.append(stripped)
    for match in _FENCE_RE.finditer(text):
        block = match.group(1).strip()
        if not block:
            continue
        if block not in candidates:
            candidates.append(block)
        inner = _first_balanced_object(block)
        if inner and inner not in candidates:
            candidates.append(inner)
    outer = _first_balanced_object(text)
    if outer and outer not in candidates:
        candidates.append(outer)
    return candidates


def extract_json(text: str) -> dict[str, Any]:
    """Extrait l'objet JSON d'une reponse modele (bloc ```json, ou objet brut)."""
    raw = text or ""
    for candidate in _json_candidates(raw):
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
        # Liste d'objets rendue sans enveloppe : « items » fait partie des cles
        # que vision._garment_entries sait lire, alors qu'un scan d'accolades ne
        # rendrait que le premier element.
        if isinstance(value, list) and any(isinstance(entry, dict) for entry in value):
            return {"items": value}
    excerpt = raw[:_MAX_RAW_CHARS]
    log.error("reponse du modele non parsable : %s", excerpt)
    raise LLMError(
        f"aucun objet JSON exploitable dans la reponse du modele : {excerpt!r}",
        user_message_fr=_PARSE_FR,
    )


def build_content(parts: Sequence[ImagePart], user_text: str) -> list[dict[str, Any]]:
    """Assemble le contenu d'un message : consigne, puis chaque libelle + image."""
    content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    for part in parts:
        if part.label:
            content.append({"type": "text", "text": part.label})
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": part.media_type,
                    "data": part.data_b64,
                },
            }
        )
    return content


class LLMBackend(Protocol):
    """Contrat attendu par le pipeline : deux appels vision qui rendent du JSON."""

    async def analyze_slides(
        self,
        images: list[ImagePart],
        *,
        system_prompt: str,
        user_text: str,
        model: str,
    ) -> dict[str, Any]:
        ...

    async def rerank(
        self,
        source: ImagePart,
        thumbnails: list[ImagePart],
        *,
        system_prompt: str,
        user_text: str,
        model: str,
    ) -> dict[str, Any]:
        ...


class _VisionBackend:
    """Les deux appels metier se ramenent au meme aller-retour vision -> JSON."""

    async def analyze_slides(
        self,
        images: list[ImagePart],
        *,
        system_prompt: str,
        user_text: str,
        model: str,
    ) -> dict[str, Any]:
        return await self._vision_json(
            list(images),
            system_prompt=system_prompt,
            user_text=user_text,
            model=model,
            max_tokens=MAX_TOKENS_ANALYSIS,
        )

    async def rerank(
        self,
        source: ImagePart,
        thumbnails: list[ImagePart],
        *,
        system_prompt: str,
        user_text: str,
        model: str,
    ) -> dict[str, Any]:
        return await self._vision_json(
            [source, *thumbnails],
            system_prompt=system_prompt,
            user_text=user_text,
            model=model,
            max_tokens=MAX_TOKENS_RERANK,
        )

    async def _vision_json(
        self,
        parts: list[ImagePart],
        *,
        system_prompt: str,
        user_text: str,
        model: str,
        max_tokens: int,
    ) -> dict[str, Any]:
        raise NotImplementedError


class AgentSDKBackend(_VisionBackend):
    """Passe par claude-agent-sdk : facture sur l'abonnement, pas sur une cle API."""

    def __init__(self, oauth_token: str, *, timeout: float = 180.0) -> None:
        # Verifie a la construction (donc au demarrage du bot) et non au premier
        # appel : une dependance absente est un probleme d'installation.
        if ClaudeSDKClient is None:
            raise LLMError(
                f"claude-agent-sdk indisponible : {_AGENT_SDK_ERROR}",
                user_message_fr=_SETUP_FR,
            )
        self._oauth_token = oauth_token
        self._timeout = timeout

    def _options(self, model: str, system_prompt: str) -> Any:
        # setting_sources=[] evite que le CLAUDE.md et les reglages personnels de
        # la machine se retrouvent dans les appels du bot.
        return ClaudeAgentOptions(
            model=model,
            system_prompt=system_prompt,
            max_turns=1,
            allowed_tools=[],
            disallowed_tools=list(_DISALLOWED_TOOLS),
            setting_sources=[],
            env={"CLAUDE_CODE_OAUTH_TOKEN": self._oauth_token},
        )

    async def _collect(self, options: Any, message: dict[str, Any]) -> str:
        async def stream() -> AsyncIterator[dict[str, Any]]:
            # Rien d'autre qu'un yield ici : une exception levee dans ce
            # generateur bloque la session sans remonter d'erreur.
            yield message

        chunks: list[str] = []
        async with ClaudeSDKClient(options=options) as client:
            await client.query(stream())
            async for response in client.receive_response():
                if isinstance(response, AssistantMessage):
                    for block in response.content:
                        if isinstance(block, TextBlock):
                            chunks.append(block.text)
        # Concatenation sans separateur : le SDK peut couper une meme reponse en
        # plusieurs blocs, y compris au milieu d'une chaine JSON, et un retour a
        # la ligne insere la rendrait invalide.
        return "".join(chunks)

    async def _vision_json(
        self,
        parts: list[ImagePart],
        *,
        system_prompt: str,
        user_text: str,
        model: str,
        max_tokens: int,
    ) -> dict[str, Any]:
        # max_tokens n'existe pas dans ClaudeAgentOptions : il est ignore ici.
        # Tout est encode avant de construire le generateur (cf. _collect).
        message = {
            "type": "user",
            "message": {"role": "user", "content": build_content(parts, user_text)},
        }
        options = self._options(model, system_prompt)

        log.info("appel Agent SDK : modele=%s images=%d", model, len(parts))
        started = time.monotonic()
        try:
            text = await asyncio.wait_for(self._collect(options, message), self._timeout)
        except TimeoutError as exc:
            raise LLMError(
                f"pas de reponse du modele en {self._timeout:.0f}s (modele {model})",
                user_message_fr=_TIMEOUT_FR,
            ) from exc
        except Exception as exc:
            # Le binaire `claude` absent ou impossible a lancer est une erreur
            # d'installation, pas un incident passager. Compare par nom : les
            # symboles d'erreur du SDK varient d'une version a l'autre.
            setup = type(exc).__name__ in {"CLINotFoundError", "CLIConnectionError"}
            raise LLMError(
                f"appel Agent SDK en echec : {exc}",
                user_message_fr=_SETUP_FR if setup else _GENERIC_FR,
            ) from exc

        log.info(
            "reponse Agent SDK en %.1fs (%d caracteres)",
            time.monotonic() - started,
            len(text),
        )
        return extract_json(text)


class AnthropicAPIBackend(_VisionBackend):
    """Passe par l'API Anthropic classique : necessite une cle et facture a l'usage."""

    def __init__(self, api_key: str, *, timeout: float = 180.0) -> None:
        if AsyncAnthropic is None:
            raise LLMError(
                f"le paquet anthropic est indisponible : {_ANTHROPIC_ERROR}",
                user_message_fr=_SETUP_FR,
            )
        self._timeout = timeout
        self._client = AsyncAnthropic(api_key=api_key, timeout=timeout)

    async def aclose(self) -> None:
        """Ferme le pool HTTP du client anthropic (a appeler a l'arret)."""
        await self._client.close()

    async def _vision_json(
        self,
        parts: list[ImagePart],
        *,
        system_prompt: str,
        user_text: str,
        model: str,
        max_tokens: int,
    ) -> dict[str, Any]:
        content = build_content(parts, user_text)

        log.info("appel API Anthropic : modele=%s images=%d", model, len(parts))
        started = time.monotonic()
        try:
            # Pas de wait_for externe : le client a deja son propre timeout par
            # tentative, et un delai global identique annulerait ses retries
            # automatiques (429, 5xx) en plein backoff.
            response = await self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": content}],
            )
        except _TIMEOUT_ERRORS as exc:
            raise LLMError(
                f"pas de reponse du modele en {self._timeout:.0f}s (modele {model})",
                user_message_fr=_TIMEOUT_FR,
            ) from exc
        except Exception as exc:
            raise LLMError(f"appel API Anthropic en echec : {exc}") from exc

        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "refusal":
            raise LLMError(
                f"le modele a refuse de repondre (modele {model})",
                user_message_fr=_REFUSAL_FR,
            )
        if stop_reason == "max_tokens":
            log.warning("reponse tronquee a %d tokens : le JSON sera incomplet", max_tokens)

        text = "\n".join(
            block.text
            for block in (getattr(response, "content", None) or [])
            if getattr(block, "type", "") == "text"
        )
        log.info(
            "reponse API en %.1fs (%d caracteres)",
            time.monotonic() - started,
            len(text),
        )
        return extract_json(text)


def build_backend(cfg: Config) -> LLMBackend:
    """Choisit le backend selon cfg.llm_backend (deja valide par load_config)."""
    if cfg.llm_backend == "agent_sdk":
        if not cfg.claude_oauth_token:
            raise LLMError(
                "CLAUDE_CODE_OAUTH_TOKEN manquant pour le backend agent_sdk",
                user_message_fr=_SETUP_FR,
            )
        return AgentSDKBackend(cfg.claude_oauth_token)
    if cfg.llm_backend == "anthropic_api":
        if not cfg.anthropic_api_key:
            raise LLMError(
                "ANTHROPIC_API_KEY manquante pour le backend anthropic_api",
                user_message_fr=_SETUP_FR,
            )
        return AnthropicAPIBackend(cfg.anthropic_api_key)
    raise LLMError(
        f"backend LLM inconnu : {cfg.llm_backend!r}",
        user_message_fr=_SETUP_FR,
    )


__all__ = [
    "AgentSDKBackend",
    "AnthropicAPIBackend",
    "LLMBackend",
    "LLMError",
    "build_backend",
    "build_content",
    "extract_json",
]
