"""Client de recherche sur l'API interne de vinted.fr (bootstrap cookies + catalog/items)."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import random
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from curl_cffi.requests import AsyncSession

from fripe.catalog import BRANDS, CATALOGS, COLORS
from fripe.models import VintedItem

log = logging.getLogger(__name__)

__all__ = [
    "BRANDS",
    "CATALOGS",
    "COLORS",
    "VintedAuthError",
    "VintedClient",
    "VintedError",
]

BASE_URL = "https://www.vinted.fr"
SEARCH_URL = f"{BASE_URL}/api/v2/catalog/items"

# curl_cffi remplit sec-ch-ua / sec-ch-ua-platform depuis le profil impersone, mais
# laisse passer notre User-Agent : les deux doivent decrire le meme navigateur (meme
# version, meme plateforme), sinon l'anti-bot de Vinted voit un UA Windows a cote
# d'un sec-ch-ua-platform "macOS". La valeur ci-dessous est celle du profil sur lequel
# "chrome" pointe dans curl_cffi 0.16 ; a re-verifier a chaque montee de version.
# Les cookies sont lies a cet UA : le changer invalide le cache disque tout seul.
CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
IMPERSONATE = "chrome"

BASE_HEADERS: dict[str, str] = {
    "User-Agent": CHROME_UA,
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
}

REQUEST_TIMEOUT = 25.0
# per_page est plafonne cote serveur : au-dela, Vinted renvoie quand meme 96 items.
MAX_PER_PAGE = 96
# access_token_web vit 24 h ; on renouvelle avant pour ne pas courir apres l'expiration.
COOKIE_MAX_AGE = 20 * 3600
SESSION_COOKIE = "access_token_web"
COOKIE_DOMAIN = ".vinted.fr"

ORDERS = frozenset(
    {"relevance", "newest_first", "price_low_to_high", "price_high_to_low"}
)

_GENERIC_FR = "La recherche sur Vinted a échoué, réessaie dans un instant."
_AUTH_FR = (
    "Vinted bloque les recherches automatiques pour le moment, "
    "réessaie dans quelques minutes."
)
_RATE_FR = "Vinted limite le rythme des recherches, réessaie dans quelques minutes."
# Apres un refus de Vinted, inutile d'insister (et de payer des analyses) :
# on refuse localement pendant ce delai.
COOLDOWN_S = 600.0


class VintedError(Exception):
    """Echec d'un appel a Vinted, avec un message pretable a l'utilisateur."""

    def __init__(self, message: str, *, user_message_fr: str = _GENERIC_FR) -> None:
        super().__init__(message)
        self.user_message_fr = user_message_fr


class VintedAuthError(VintedError):
    """Vinted refuse toujours la session apres un renouvellement des cookies."""

    def __init__(self, message: str, *, user_message_fr: str = _AUTH_FR) -> None:
        super().__init__(message, user_message_fr=user_message_fr)


class VintedClient:
    """Client asynchrone : une session curl_cffi, des cookies caches, des appels espaces."""

    def __init__(
        self,
        cookie_cache: Path,
        *,
        min_delay: float = 0.8,
        max_delay: float = 1.8,
    ) -> None:
        self.cookie_cache = Path(cookie_cache)
        self.min_delay = max(0.0, float(min_delay))
        self.max_delay = max(self.min_delay, float(max_delay))

        self._session: AsyncSession | None = None
        self._ready_at: float = 0.0
        self._blocked_until: float = 0.0
        self._generation: int = 0
        self._cache_loaded = False
        # Ordre d'acquisition impose : bootstrap puis appel, jamais l'inverse.
        self._bootstrap_lock = asyncio.Lock()
        self._call_lock = asyncio.Lock()

    async def __aenter__(self) -> "VintedClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def search(
        self,
        query: str,
        *,
        catalog_ids: Sequence[int] | None = None,
        color_ids: Sequence[int] | None = None,
        brand_ids: Sequence[int] | None = None,
        price_to: int | None = None,
        per_page: int = 32,
        page: int = 1,
        order: str = "relevance",
    ) -> list[VintedItem]:
        """Cherche des annonces. Une liste vide est un resultat valide, pas une erreur."""
        params = _build_params(
            query,
            catalog_ids=catalog_ids,
            color_ids=color_ids,
            brand_ids=brand_ids,
            price_to=price_to,
            per_page=per_page,
            page=page,
            order=order,
        )

        self._check_cooldown()
        generation = await self._ensure_cookies()
        status, payload = await self._call_api(params)

        if status in (401, 403):
            log.info("Vinted repond %s : renouvellement des cookies et nouvel essai.", status)
            await self._ensure_cookies(stale_generation=generation)
            status, payload = await self._call_api(params)
            if status in (401, 403):
                self._block()
                raise VintedAuthError(
                    f"HTTP {status} sur {SEARCH_URL} apres renouvellement des cookies"
                )

        if status == 429:
            self._block()
            raise VintedError(f"HTTP 429 sur {SEARCH_URL}", user_message_fr=_RATE_FR)
        if status != 200:
            raise VintedError(f"HTTP {status} inattendu sur {SEARCH_URL}")
        if payload is None:
            raise VintedError(f"reponse illisible (JSON invalide) de {SEARCH_URL}")

        items = _parse_items(payload)
        log.info(
            "Vinted: %d annonce(s) pour %r (params: %s)",
            len(items),
            params.get("search_text", ""),
            {k: v for k, v in params.items() if k not in {"search_text", "currency"}},
        )
        return items

    async def ensure_ready(self) -> None:
        """Verifie a bas cout que Vinted accepte la session (cookies), sans chercher."""
        self._check_cooldown()
        await self._ensure_cookies()

    def _check_cooldown(self) -> None:
        remaining = self._blocked_until - time.time()
        if remaining <= 0:
            return
        minutes = int(remaining // 60) + 1
        raise VintedError(
            f"Vinted en pause encore {minutes} min apres un refus",
            user_message_fr=(
                "Vinted bloque les recherches automatiques pour le moment, je n'ai pas "
                f"lancé l'analyse. Renvoie le lien dans {minutes} min."
            ),
        )

    def _block(self) -> None:
        self._blocked_until = time.time() + COOLDOWN_S
        log.warning("Vinted refuse la session : pause de %.0f min", COOLDOWN_S / 60)

    async def close(self) -> None:
        session, self._session = self._session, None
        self._ready_at = 0.0
        if session is None:
            return
        try:
            # close() est une coroutine sur AsyncSession, une methode simple ailleurs.
            result = session.close()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001
            log.debug("Fermeture de la session Vinted en erreur : %s", exc)

    async def _call_api(self, params: Mapping[str, str]) -> tuple[int, Any | None]:
        session = self._session
        if session is None:
            raise VintedError("session Vinted non initialisee")

        await self._throttle()
        headers = {"Accept": "application/json", "Referer": f"{BASE_URL}/catalog"}
        try:
            response = await session.get(SEARCH_URL, params=dict(params), headers=headers)
        except Exception as exc:  # noqa: BLE001
            raise VintedError(f"appel Vinted impossible : {exc}") from exc

        status = int(getattr(response, "status_code", 0) or 0)
        if status != 200:
            return status, None
        try:
            return status, response.json()
        except Exception as exc:  # noqa: BLE001
            log.debug("JSON Vinted illisible : %s", exc)
            return status, None

    async def _throttle(self) -> None:
        # Le verrou est tenu pendant la pause : les appels concurrents s'espacent
        # au lieu de partir en rafale.
        async with self._call_lock:
            delay = random.uniform(self.min_delay, self.max_delay)
            if delay > 0:
                await asyncio.sleep(delay)

    async def _ensure_cookies(self, *, stale_generation: int | None = None) -> int:
        async with self._bootstrap_lock:
            self._open_session()
            if stale_generation is None:
                if not self._fresh() and not self._cache_loaded:
                    self._cache_loaded = True
                    self._load_cache()
                if not self._fresh():
                    await self._bootstrap()
            elif stale_generation == self._generation:
                await self._bootstrap()
            return self._generation

    def _open_session(self) -> None:
        if self._session is None:
            self._session = AsyncSession(
                impersonate=IMPERSONATE,
                headers=dict(BASE_HEADERS),
                timeout=REQUEST_TIMEOUT,
            )

    def _fresh(self) -> bool:
        return self._ready_at > 0.0 and (time.time() - self._ready_at) < COOKIE_MAX_AGE

    async def _bootstrap(self) -> None:
        """Visite la page d'accueil : Vinted y depose access_token_web et ses compagnons."""
        session = self._session
        if session is None:
            raise VintedError("session Vinted non initialisee")

        self._ready_at = 0.0
        self._clear_jar()

        await self._throttle()
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Upgrade-Insecure-Requests": "1",
        }
        try:
            response = await session.get(f"{BASE_URL}/", headers=headers)
        except Exception as exc:  # noqa: BLE001
            raise VintedError(f"bootstrap Vinted impossible : {exc}") from exc

        status = int(getattr(response, "status_code", 0) or 0)
        if status >= 400:
            if status in (401, 403, 429):
                self._block()
            raise VintedError(
                f"HTTP {status} sur {BASE_URL}/ pendant le bootstrap",
                user_message_fr=(
                    _RATE_FR
                    if status == 429
                    else _AUTH_FR
                    if status in (401, 403)
                    else _GENERIC_FR
                ),
            )

        self._ready_at = time.time()
        self._generation += 1

        # Un snapshot vide n'est pas bloquant : curl garde les cookies dans sa
        # propre session, seul le cache disque est perdu.
        cookies = self._jar_snapshot()
        if cookies.get(SESSION_COOKIE):
            self._save_cache(cookies)
        else:
            log.debug("Bootstrap Vinted sans %s lisible : pas de mise en cache.", SESSION_COOKIE)

    def _load_cache(self) -> None:
        try:
            raw = json.loads(self.cookie_cache.read_text(encoding="utf-8"))
            saved_at = float(raw.get("saved_at") or 0.0)
            user_agent = str(raw.get("user_agent") or "")
            cookies = {
                str(name): str(value)
                for name, value in (raw.get("cookies") or {}).items()
                if name and value
            }
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            log.debug("Cache de cookies Vinted inutilisable : %s", exc)
            return

        if user_agent != CHROME_UA:
            log.debug("Cache de cookies Vinted lie a un autre User-Agent : ignore.")
            return
        if not cookies.get(SESSION_COOKIE):
            log.debug("Cache de cookies Vinted sans %s : ignore.", SESSION_COOKIE)
            return
        if (time.time() - saved_at) >= COOKIE_MAX_AGE:
            log.debug("Cache de cookies Vinted trop ancien : ignore.")
            return
        if not self._apply_to_jar(cookies):
            return

        self._ready_at = saved_at
        self._generation += 1
        log.debug("Cookies Vinted repris du cache (%s).", self.cookie_cache)

    def _save_cache(self, cookies: Mapping[str, str]) -> None:
        payload = {
            "saved_at": self._ready_at,
            "user_agent": CHROME_UA,
            "cookies": dict(cookies),
        }
        tmp = self.cookie_cache.with_name(self.cookie_cache.name + ".tmp")
        try:
            self.cookie_cache.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.cookie_cache)
        except (OSError, TypeError, ValueError) as exc:
            log.debug("Ecriture du cache de cookies Vinted impossible : %s", exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _apply_to_jar(self, cookies: Mapping[str, str]) -> bool:
        """Reinjecte des cookies caches dans la session ; False = repartir sur un bootstrap."""
        session = self._session
        if session is None:
            return False
        try:
            session.cookies.update(dict(cookies))
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("cookies.update refuse par curl_cffi (%s), essai cookie par cookie.", exc)
        try:
            for name, value in cookies.items():
                session.cookies.set(name, value, domain=COOKIE_DOMAIN, path="/")
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("Cookies Vinted non reinjectables (%s) : bootstrap complet.", exc)
            self._clear_jar()
            return False

    def _jar_snapshot(self) -> dict[str, str]:
        """Lit le jar sous forme {nom: valeur}, sans jamais faire echouer l'appelant."""
        session = self._session
        if session is None:
            return {}
        try:
            return {
                str(name): str(value)
                for name, value in dict(session.cookies).items()
                if name and value
            }
        except Exception as exc:  # noqa: BLE001
            log.debug("Lecture directe du jar impossible (%s), passage par jar.", exc)
        try:
            return {
                str(cookie.name): str(cookie.value)
                for cookie in session.cookies.jar
                if cookie.name and cookie.value
            }
        except Exception as exc:  # noqa: BLE001
            log.debug("Jar de cookies Vinted illisible : %s", exc)
            return {}

    def _clear_jar(self) -> None:
        session = self._session
        if session is None:
            return
        try:
            session.cookies.clear()
        except Exception as exc:  # noqa: BLE001
            log.debug("Purge du jar de cookies impossible : %s", exc)


def search_url(
    query: str,
    *,
    catalog_id: int | None = None,
    color_ids: Sequence[int] | None = None,
    price_to: int | None = None,
) -> str:
    """Adresse de la meme recherche sur vinted.fr, a ouvrir dans l'appli ou le navigateur."""
    params: list[tuple[str, str]] = [("search_text", query)]
    if catalog_id:
        params.append(("catalog[]", str(catalog_id)))
    for color in color_ids or []:
        params.append(("color_ids[]", str(color)))
    if price_to:
        params.append(("price_to", str(price_to)))
    return f"{BASE_URL}/catalog?{urlencode(params)}"


def _build_params(
    query: str,
    *,
    catalog_ids: Sequence[int] | None,
    color_ids: Sequence[int] | None,
    brand_ids: Sequence[int] | None,
    price_to: int | None,
    per_page: int,
    page: int,
    order: str,
) -> dict[str, str]:
    try:
        wanted = int(per_page)
    except (TypeError, ValueError):
        wanted = 32
    try:
        wanted_page = int(page)
    except (TypeError, ValueError):
        wanted_page = 1

    params: dict[str, str] = {
        "page": str(max(1, wanted_page)),
        "per_page": str(max(1, min(MAX_PER_PAGE, wanted))),
        "currency": "EUR",
        "order": order if order in ORDERS else "relevance",
    }
    if order not in ORDERS:
        log.debug("Ordre Vinted inconnu %r : relevance utilise a la place.", order)

    text = " ".join(str(query or "").split())
    if text:
        params["search_text"] = text

    # Un filtre vide envoye a vide (catalog_ids=) fait repondre n'importe quoi a
    # Vinted : on omet purement la cle.
    for key, values in (
        ("catalog_ids", catalog_ids),
        ("color_ids", color_ids),
        ("brand_ids", brand_ids),
    ):
        joined = _join_ids(values)
        if joined:
            params[key] = joined

    if price_to is not None:
        try:
            limit = int(price_to)
        except (TypeError, ValueError):
            limit = 0
        if limit > 0:
            params["price_to"] = str(limit)

    return params


def _join_ids(values: Sequence[int] | None) -> str | None:
    if not values:
        return None
    seen: list[str] = []
    for value in values:
        try:
            text = str(int(value))
        except (TypeError, ValueError):
            continue
        if text not in seen:
            seen.append(text)
    return ",".join(seen) or None


def _parse_items(payload: Any) -> list[VintedItem]:
    if not isinstance(payload, dict):
        raise VintedError("reponse Vinted inattendue (objet JSON attendu)")
    raw_items = payload.get("items")
    if raw_items is None:
        raise VintedError("reponse Vinted sans champ 'items'")
    if not isinstance(raw_items, list):
        raise VintedError("champ 'items' inattendu dans la reponse Vinted")

    items: list[VintedItem] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        try:
            items.append(VintedItem.from_api(raw))
        except Exception as exc:  # noqa: BLE001
            # ValidationError, cle absente, type surprenant : une annonce bancale
            # ne doit pas faire tomber toute la recherche.
            log.debug("Annonce Vinted ignoree (%s) : %.200s", exc, raw)
    return items
