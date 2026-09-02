"""Bot Telegram : recoit un lien TikTok, repond avec les annonces Vinted."""

from __future__ import annotations

import asyncio
import html
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from telegram import InputMediaPhoto, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Conflict, InvalidToken, NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from fripe.config import Config, ConfigError, load_config, mask_secrets, setup_logging
from fripe.llm import LLMError, build_backend
from fripe.models import Garment, GarmentResult, VintedItem
from fripe.pipeline import Deps, process_link, sweep_stale_slides
from fripe.rerank import build_reranker
from fripe.tiktok import NotATikTokUrl, TikTokError, find_tiktok_urls
from fripe.vinted import VintedClient, VintedError, search_url
from fripe.vision import VisionError

# Nom fixe : lance par `python -m`, __name__ vaudrait "__main__" dans le journal.
log = logging.getLogger("fripe.bot")

# Telegram plafonne les legendes a 1024 caracteres.
CAPTION_LIMIT = 1024
# Deux recherches simultanees au maximum : protege les quotas tikwm/Vinted et la
# memoire d'un Raspberry Pi.
GLOBAL_CONCURRENCY = 2
# Au-dela de ce delai entre l'envoi d'un message et sa lecture, le bot etait
# manifestement eteint : on le dit, sinon la reponse tardive surprend.
LATE_AFTER = timedelta(minutes=10)
# Un lien renvoye « pour etre sur » pendant que le bot ne repondait pas ne doit
# pas payer une deuxieme analyse : meme lien, meme chat, dans cette fenetre.
DEDUP_WINDOW_S = 30 * 60
# Pendant une recherche, l'horloge monotone s'arrete si la machine dort alors
# que l'horloge murale continue : un ecart trahit une veille, et donc des
# connexions mortes. On recommence une fois plutot que d'accuser le lien.
SLEEP_GAP_S = 60
# Telegram garde 24 h les messages recus pendant que le bot est eteint : on les
# rattrape au demarrage au lieu de les jeter. Et au reveil d'une machine, le
# reseau arrive souvent apres nous : PTB reessaie alors indefiniment (jusqu'a
# toutes les 30 s) au lieu de planter et de laisser launchd relancer en boucle.
POLLING_OPTIONS = {"drop_pending_updates": False, "bootstrap_retries": -1}
# Code de sortie des arrets definitifs (configuration invalide, jeton refuse) :
# launchd ne relance pas un processus sorti en 0 (KeepAlive SuccessfulExit=false),
# alors qu'un plantage (code != 0) est bien relance.
EXIT_DEFINITIVE = 0
# launchd n'effectue aucune rotation de son propre fichier de sortie.
LAUNCHD_LOG_MAX = 1_000_000
LAUNCHD_LOG_KEEP = 200_000
# Un delai (RetryAfter) est rejoue une fois ; au-dela, on abandonne l'envoi.
_NO_PREVIEW = {"link_preview_options": LinkPreviewOptions(is_disabled=True)}

HELP_FR = (
    "Salut ! 👋\n\n"
    "Envoie-moi le lien d'un TikTok en mode photo (un diaporama de tenues) et je "
    "cherche chaque pièce sur Vinted.\n\n"
    "Sur TikTok : <b>Partager</b> → <b>Copier le lien</b>, puis colle-le ici.\n\n"
    "⚠️ Je ne traite que les diaporamas photo, pas les vidéos."
)
HINT_FR = (
    "Je ne lis que les liens TikTok en mode photo 🙂 Sur TikTok : "
    "<b>Partager</b> → <b>Copier le lien</b>, puis colle-le ici."
)
BAD_LINK_FR = (
    "Je ne reconnais pas ce lien TikTok 🤔 Renvoie-moi celui de "
    "<b>Partager</b> → <b>Copier le lien</b>."
)
RESTARTING_FR = "⚠️ Je redémarre, renvoie ton lien dans une minute 🙏"
DUPLICATE_FR = "Je m'occupe déjà de ce lien 😉 (ou je viens de te l'envoyer)"
NOT_CONFIGURED_FR = (
    "🔒 Ce bot est privé et pas encore configuré.\n"
    "Ton identifiant : <code>{chat_id}</code>\n"
    "Ajoute-le à ALLOWED_CHAT_IDS dans le fichier .env, puis relance le bot "
    "(./autostart.sh restart)."
)
PRIVATE_FR = (
    "Ce bot est privé 🙈 Demande à son propriétaire de t'ajouter "
    "(identifiant de ce chat : {chat_id})."
)
NO_GARMENT_FR = "🤔 Je n'ai reconnu aucun vêtement sur ces photos."


def _chat_locks() -> defaultdict[int, asyncio.Lock]:
    return defaultdict(asyncio.Lock)


def ack_text(sent_at: datetime | None, now: datetime | None = None, *, count: int = 1) -> str:
    """Accuse de reception ; signale un lien recu pendant que le bot etait eteint.

    Telegram fournit toujours une date UTC consciente du fuseau.
    """
    now = now or datetime.now(timezone.utc)
    late = sent_at is not None and now - sent_at > LATE_AFTER
    prefix = "🔎 Reçu pendant que j'étais éteint, je m'en occupe maintenant. " if late else "🔎 "
    if count > 1:
        return prefix + f"J'ai trouvé {count} liens, je les traite l'un après l'autre."
    return prefix + "Je récupère les photos…"


async def _post_init(app: Application) -> None:
    cfg: Config = app.bot_data["cfg"]
    http = httpx.AsyncClient(
        timeout=20.0,
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0.0.0"},
        follow_redirects=True,
    )
    backend = build_backend(cfg)
    app.bot_data["deps"] = Deps(
        vinted=VintedClient(cookie_cache=cfg.data_dir / "vinted_cookies.json"),
        backend=backend,
        reranker=build_reranker(cfg, backend, http),
        http=http,
    )
    app.bot_data["locks"] = _chat_locks()
    app.bot_data["semaphore"] = asyncio.Semaphore(GLOBAL_CONCURRENCY)
    app.bot_data["recents"] = {}
    app.bot_data["net_error_at"] = 0.0

    # Un arret brutal (SIGKILL, coupure) saute le nettoyage du pipeline.
    removed = sweep_stale_slides(cfg.data_dir / "slides")
    if removed:
        log.info("%d dossier(s) de slides orphelin(s) supprime(s)", removed)

    if cfg.open_to_all:
        log.warning("ALLOWED_CHAT_IDS=* : n'importe qui peut utiliser ce bot et consommer ton credit")
    elif not cfg.allowed_chat_ids:
        log.warning(
            "ALLOWED_CHAT_IDS est vide : le bot refuse tout le monde tant que ton "
            "identifiant n'y est pas (envoie-lui /id)"
        )
    # Pas d'appel reseau ici : post_init n'est pas couvert par les reprises de
    # PTB, et l'identite du bot est deja connue depuis l'initialisation.
    log.info("bot @%s pret (backend=%s)", app.bot.username, cfg.llm_backend)


async def _post_shutdown(app: Application) -> None:
    deps: Deps | None = app.bot_data.get("deps")
    if deps is None:
        return
    await deps.vinted.close()
    # Le backend API possede son propre pool HTTP ; celui de l'Agent SDK non.
    closer = getattr(deps.backend, "aclose", None)
    if closer is not None:
        await closer()
    await deps.http.aclose()


async def on_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(HELP_FR, parse_mode=ParseMode.HTML)


async def on_id(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Affiche l'identifiant du chat, a mettre dans ALLOWED_CHAT_IDS."""
    if update.effective_chat and update.message:
        await update.message.reply_text(f"Identifiant de ce chat : {update.effective_chat.id}")


def _seen_recently(recents: dict, chat_id: int, url: str) -> bool:
    now = time.monotonic()
    for key, stamp in list(recents.items()):
        if now - stamp > DEDUP_WINDOW_S:
            del recents[key]
    return (chat_id, url) in recents


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    chat = update.effective_chat
    if message is None or chat is None:
        return
    # Une photo avec le lien en legende est un usage naturel sur telephone.
    text = message.text or message.caption or ""
    if not text:
        return

    cfg: Config = ctx.application.bot_data["cfg"]
    if not cfg.is_allowed(chat.id):
        log.warning("chat non autorise : %s", chat.id)
        template = PRIVATE_FR if cfg.allowed_chat_ids else NOT_CONFIGURED_FR
        await _safe_send(message, template.format(chat_id=chat.id))
        return

    urls = find_tiktok_urls(text)
    if not urls:
        await _safe_send(message, BAD_LINK_FR if "tiktok" in text.lower() else HINT_FR)
        return

    if not ctx.application.running:
        # Arret en cours : une tache creee maintenant ne serait jamais attendue
        # et mourrait avec le processus, accuse de reception deja envoye.
        await _safe_send(message, RESTARTING_FR)
        return

    recents: dict = ctx.application.bot_data.setdefault("recents", {})
    fresh = [url for url in urls if not _seen_recently(recents, chat.id, url)]
    if not fresh:
        await _safe_send(message, DUPLICATE_FR)
        return
    for url in fresh:
        recents[(chat.id, url)] = time.monotonic()

    # Un seul accuse de reception, meme pour plusieurs liens : au rattrapage
    # d'un lot, une rafale de messages vers un meme chat finit en 429.
    status = await _safe_send(message, ack_text(message.date, count=len(fresh)))
    for index, url in enumerate(fresh):
        ctx.application.create_task(
            run_job(update, ctx, url, status if index == 0 else None),
            update=update,
        )


async def run_job(update: Update, ctx: ContextTypes.DEFAULT_TYPE, url: str, status) -> None:
    cfg: Config = ctx.application.bot_data["cfg"]
    deps: Deps = ctx.application.bot_data["deps"]
    locks: defaultdict[int, asyncio.Lock] = ctx.application.bot_data["locks"]
    semaphore: asyncio.Semaphore = ctx.application.bot_data["semaphore"]
    recents: dict = ctx.application.bot_data.setdefault("recents", {})
    chat_id = update.effective_chat.id if update.effective_chat else 0

    async def progress(text: str) -> None:
        await _edit(status, text)

    if locks[chat_id].locked():
        await _edit(status, "⏳ Je termine d'abord ta recherche précédente…")

    async with locks[chat_id], semaphore:
        keep_awake = await _keep_awake()
        try:
            results = await _run_pipeline(url, cfg, deps, progress, status)
        except asyncio.CancelledError:
            await _edit(status, "⏹ Interrompu par un redémarrage, renvoie le lien 🙏")
            raise
        except (NotATikTokUrl, TikTokError, VintedError, LLMError, VisionError) as exc:
            user_message = getattr(exc, "user_message_fr", None) or "Ça n'a pas marché 😕"
            log.info("echec utilisateur (%s) : %s", type(exc).__name__, exc)
            await _edit(status, user_message)
            return
        except Exception:
            log.exception("echec inattendu du pipeline")
            await _edit(status, "Oups, quelque chose a cassé de mon côté 😅 Réessaie plus tard.")
            return
        finally:
            await _release(keep_awake)
            recents[(chat_id, url)] = time.monotonic()

        if not results:
            await _edit(status, NO_GARMENT_FR)
            return

        # Le statut reste affiche pendant l'envoi : un arret brutal laisse au
        # moins un message coherent plutot qu'un chat sans explication.
        await _edit(status, "📦 J'envoie les résultats…")
        # Un envoi rate (reseau, flood) ne doit pas emporter les albums
        # suivants : l'analyse a deja ete payee pour chacun d'eux.
        rates = 0
        for result in results:
            try:
                await send_garment_album(update.message, result, deps.http)
            except Exception:
                rates += 1
                log.exception("envoi de l'album %r en echec", result.garment.label_fr)
        await _delete(status)
        if rates:
            await _safe_send(update.message, f"⚠️ {rates} album(s) n'ont pas pu être envoyés, désolé.")


async def _run_pipeline(url: str, cfg: Config, deps: Deps, progress, status):
    wall, mono = time.time(), time.monotonic()
    try:
        return await process_link(url, cfg, deps, progress)
    except (TikTokError, VintedError, LLMError) as exc:
        slept = (time.time() - wall) - (time.monotonic() - mono)
        if slept < SLEEP_GAP_S:
            raise
        log.info(
            "la machine a dormi ~%.0f s pendant la recherche (%s) : nouvel essai",
            slept,
            type(exc).__name__,
        )
        await _edit(status, "💤 Le Mac s'est endormi pendant la recherche, je recommence…")
        return await process_link(url, cfg, deps, progress)


async def _keep_awake():
    """Sur Mac, empeche la veille d'inactivite le temps d'une recherche (15 min max)."""
    if sys.platform != "darwin":
        return None
    try:
        return await asyncio.create_subprocess_exec(
            "caffeinate", "-i", "-t", "900",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None


async def _release(process) -> None:
    if process is None or process.returncode is not None:
        return
    try:
        process.terminate()
        await asyncio.wait_for(process.wait(), 5)
    except Exception:
        log.debug("caffeinate ne s'est pas arrete proprement", exc_info=True)


async def _safe_send(message, text: str):
    """Envoie une reponse sans jamais faire echouer le handler ; None si impossible."""
    if message is None:
        return None
    try:
        return await _with_retry(lambda: message.reply_text(text, parse_mode=ParseMode.HTML))
    except Exception:
        log.warning("reponse Telegram impossible", exc_info=True)
        return None


async def _edit(status, text: str) -> None:
    if status is None:
        return
    try:
        await _with_retry(lambda: status.edit_text(text))
    except BadRequest as exc:
        # Le meme texte deux fois n'est pas une erreur qui merite une trace.
        if "not modified" not in str(exc).lower():
            log.debug("edition du statut refusee : %s", exc)
    except Exception:
        log.debug("edition du statut impossible", exc_info=True)


async def _delete(status) -> None:
    if status is None:
        return
    try:
        await status.delete()
    except Exception:
        log.debug("suppression du message de statut impossible", exc_info=True)


def album_items(result: GarmentResult) -> list[VintedItem]:
    """Les annonces avec photo d'abord : l'ordre des medias, donc de la numerotation."""
    with_photo = [item for item in result.items if item.photo_url]
    without = [item for item in result.items if not item.photo_url]
    return with_photo + without


def garment_search_url(garment: Garment) -> str:
    query = garment.queries_fr[0] if garment.queries_fr else garment.label_fr
    return search_url(query, catalog_id=garment.catalog_id, color_ids=garment.color_ids)


def _link(url: str, label: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'


def build_caption(result: GarmentResult) -> str:
    """Legende HTML de l'album : une ligne par annonce, tronquee a 1024 caracteres."""
    header = f"🧵 <b>{html.escape(result.garment.label_fr)}</b>"
    ordered = album_items(result)
    if ordered:
        pluriel = "s" if len(ordered) > 1 else ""
        header += f" — {len(ordered)} annonce{pluriel}"
    if result.note_fr:
        header += f"\n<i>{html.escape(result.note_fr)}</i>"

    # Le lien de secours vers Vinted reste utile quand l'album deçoit ; sa
    # place est reservee avant de remplir, ainsi que celle du « … et N autres ».
    footer = "🔗 " + _link(garment_search_url(result.garment), "Voir plus sur Vinted") if ordered else ""
    budget = CAPTION_LIMIT - (len(footer) + 1 if footer else 0) - len("\n… et 99 autres")

    lines = [header]
    for position, item in enumerate(ordered, start=1):
        title = item.title[:40].strip()
        details = [item.price_label()]
        if item.brand_title:
            details.append(html.escape(item.brand_title))
        if item.size_title:
            details.append(html.escape(item.size_title))
        if item.status:
            details.append(html.escape(item.status))
        if not item.photo_url:
            details.append("sans photo")
        line = f"{position}. " + _link(item.url, title) + " — " + " · ".join(details)
        if sum(len(part) + 1 for part in lines) + len(line) > budget:
            rest = len(ordered) - position + 1
            lines.append(f"… et {rest} autre{'s' if rest > 1 else ''}")
            break
        lines.append(line)
    if footer:
        lines.append(footer)
    return "\n".join(lines)


def no_result_text(result: GarmentResult) -> str:
    """Message « rien trouve » : les requetes essayees, cliquables sur Vinted."""
    garment = result.garment
    queries = garment.queries_fr or [garment.label_fr]
    lines = [f"🧵 <b>{html.escape(garment.label_fr)}</b>"]
    if result.note_fr:
        lines.append(f"<i>{html.escape(result.note_fr)}</i>")
    lines.append("Rien trouvé sur Vinted 😕 Ouvre ces recherches, il y a peut-être du nouveau :")
    for query in queries:
        url = search_url(query, catalog_id=garment.catalog_id, color_ids=garment.color_ids)
        lines.append("🔗 " + _link(url, f"« {query} »"))
    lines.append("Sinon, essaie avec des photos plus nettes.")
    return "\n".join(lines)


async def _download_photo(url: str | None, http: httpx.AsyncClient) -> bytes | None:
    if not url:
        return None
    try:
        response = await http.get(url)
        response.raise_for_status()
        return response.content
    except Exception:
        log.debug("photo non telechargeable : %s", url, exc_info=True)
        return None


def _build_media(photos: list, caption: str) -> list[InputMediaPhoto]:
    """La legende ne s'affiche sous l'album que si un seul media la porte."""
    return [
        InputMediaPhoto(
            media=photo,
            caption=caption if index == 0 else None,
            parse_mode=ParseMode.HTML if index == 0 else None,
        )
        for index, photo in enumerate(photos)
    ]


async def send_garment_album(message, result: GarmentResult, http: httpx.AsyncClient) -> None:
    """Envoie l'album d'un vetement, avec repli si Telegram refuse les URLs."""
    if message is None:
        return

    if not result.items:
        await message.reply_text(no_result_text(result), parse_mode=ParseMode.HTML, **_NO_PREVIEW)
        return

    caption = build_caption(result)
    items = [item for item in album_items(result) if item.photo_url]
    if len(items) < 2:
        # De preference l'item qui a une photo, meme s'il n'est pas premier.
        await _send_single(message, items[0] if items else result.items[0], caption, http)
        return

    try:
        await _with_retry(lambda: message.reply_media_group(_build_media([i.photo_url for i in items], caption)))
        return
    except BadRequest:
        # Une seule URL refusee fait echouer tout l'album : on re-televerse les
        # images en octets, ce qui contourne aussi les CDN qui bloquent Telegram.
        log.info("album par URL refuse, nouvel essai en televersant les images")

    downloaded = await asyncio.gather(*(_download_photo(i.photo_url, http) for i in items))
    payloads = [data for data in downloaded if data]
    if len(payloads) >= 2:
        try:
            await _with_retry(lambda: message.reply_media_group(_build_media(payloads, caption)))
            return
        except BadRequest:
            log.info("album televerse refuse aussi, envoi photo par photo")

    sent_caption = False
    for item in items:
        try:
            await _with_retry(
                lambda item=item: message.reply_photo(
                    photo=item.photo_url,
                    caption=None if sent_caption else caption,
                    parse_mode=None if sent_caption else ParseMode.HTML,
                )
            )
            sent_caption = True
        except BadRequest:
            log.debug("photo refusee : %s", item.photo_url)
    if not sent_caption:
        await message.reply_text(caption, parse_mode=ParseMode.HTML, **_NO_PREVIEW)


async def _send_single(message, item, caption: str, http: httpx.AsyncClient) -> None:
    if item.photo_url:
        try:
            await _with_retry(
                lambda: message.reply_photo(
                    photo=item.photo_url, caption=caption, parse_mode=ParseMode.HTML
                )
            )
            return
        except BadRequest:
            log.info("photo unique refusee par URL, nouvel essai en televersant")
        data = await _download_photo(item.photo_url, http)
        if data:
            try:
                await _with_retry(
                    lambda: message.reply_photo(
                        photo=data, caption=caption, parse_mode=ParseMode.HTML
                    )
                )
                return
            except BadRequest:
                log.info("photo unique refusee aussi en televersant, repli en texte")
    await message.reply_text(caption, parse_mode=ParseMode.HTML, **_NO_PREVIEW)


async def _with_retry(action):
    """Rejoue une action une fois si Telegram impose une pause (flood control)."""
    try:
        return await action()
    except RetryAfter as exc:
        # PTB expose le delai en secondes aujourd'hui, en timedelta demain :
        # l'attribut prive est deja un timedelta, sans avertissement.
        delay = getattr(exc, "_retry_after", None)
        seconds = delay.total_seconds() if delay is not None else float(exc.retry_after)
        await asyncio.sleep(seconds + 0.5)
        return await action()


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Une ligne pour les coupures reseau du polling, une trace pour le reste."""
    error = ctx.error
    if update is None and isinstance(error, (NetworkError, TimedOut, Conflict)):
        if isinstance(error, Conflict):
            text = (
                "un autre bot écoute déjà ce jeton Telegram (409) : "
                "un ./start.sh tourne encore quelque part ?"
            )
        else:
            text = f"Telegram injoignable ({type(error).__name__}), nouvel essai automatique"
        data = ctx.application.bot_data
        # Le polling reessaie toutes les 30 s au plus : une ligne toutes les
        # cinq minutes suffit a dire que l'on attend le reseau.
        if time.monotonic() - data.get("net_error_at", 0.0) > 300:
            data["net_error_at"] = time.monotonic()
            log.warning(text)
        return
    log.error("erreur non gérée", exc_info=error)


def _trim_launchd_log() -> None:
    """launchd ne tourne jamais son fichier de sortie : on le borne nous-memes."""
    log_file = os.environ.get("FRIPE_LOG_FILE")
    if not log_file:
        return
    path = Path(log_file).with_name("launchd.log")
    try:
        if path.stat().st_size > LAUNCHD_LOG_MAX:
            path.write_bytes(path.read_bytes()[-LAUNCHD_LOG_KEEP:])
    except OSError:
        pass


def main() -> None:
    # Journal, cookies et slides ne regardent que ce compte.
    os.umask(0o077)
    setup_logging()
    _trim_launchd_log()
    try:
        cfg = load_config()
    except ConfigError as exc:
        # Dans le journal plutot que sur stderr : c'est la que l'on regarde
        # quand le bot tourne en arriere-plan.
        log.error("Configuration invalide : %s", exc)
        log.error("Arrêt : corrige le fichier .env puis relance (./autostart.sh restart).")
        raise SystemExit(EXIT_DEFINITIVE) from exc
    # Les jetons ne doivent jamais apparaitre dans le journal, meme via une
    # trace de bibliotheque (PTB cite le jeton Telegram quand il est refuse).
    mask_secrets([cfg.telegram_token, cfg.claude_oauth_token, cfg.anthropic_api_key])

    app = (
        ApplicationBuilder()
        .token(cfg.telegram_token)
        .media_write_timeout(60)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.bot_data["cfg"] = cfg
    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("help", on_start))
    app.add_handler(CommandHandler("id", on_id))
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)
    # PTB ne raconte ses reprises reseau qu'en DEBUG pendant la connexion
    # initiale : cette ligne, restee sans « pret » derriere, dit qu'on attend
    # le Wi-Fi (reveil de la machine). Une fois connecte, voir on_error.
    log.info("connexion à Telegram…")
    try:
        app.run_polling(**POLLING_OPTIONS)
    except InvalidToken as exc:
        # Sans le detail de l'exception : PTB y recopie le jeton en clair.
        log.error(
            "Telegram refuse le jeton TELEGRAM_BOT_TOKEN : vérifie le fichier .env "
            "ou relance ./install.sh."
        )
        raise SystemExit(EXIT_DEFINITIVE) from exc


if __name__ == "__main__":
    main()
