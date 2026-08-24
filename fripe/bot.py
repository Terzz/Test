"""Bot Telegram : recoit un lien TikTok, repond avec les annonces Vinted."""

from __future__ import annotations

import asyncio
import html
import logging
from collections import defaultdict

import httpx
from telegram import InputMediaPhoto, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from fripe.config import Config, ConfigError, load_config, setup_logging
from fripe.llm import LLMError, build_backend
from fripe.models import GarmentResult
from fripe.pipeline import Deps, process_link
from fripe.rerank import build_reranker
from fripe.tiktok import NotATikTokUrl, TikTokError, find_tiktok_url
from fripe.vinted import VintedClient, VintedError
from fripe.vision import VisionError

log = logging.getLogger(__name__)

# Telegram plafonne les legendes a 1024 caracteres.
CAPTION_LIMIT = 1024
# Deux recherches simultanees au maximum : protege les quotas tikwm/Vinted et la
# memoire d'un Raspberry Pi.
GLOBAL_CONCURRENCY = 2

HELP_FR = (
    "Salut ! 👋\n\n"
    "Envoie-moi le lien d'un TikTok en mode photo (un diaporama de tenues) et je "
    "cherche chaque pièce sur Vinted.\n\n"
    "Sur TikTok : <b>Partager</b> → <b>Copier le lien</b>, puis colle-le ici.\n\n"
    "⚠️ Je ne traite que les diaporamas photo, pas les vidéos."
)


def _chat_locks() -> defaultdict[int, asyncio.Lock]:
    return defaultdict(asyncio.Lock)


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
    me = await app.bot.get_me()
    log.info("bot @%s pret (backend=%s)", me.username, cfg.llm_backend)


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


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    chat = update.effective_chat
    if message is None or chat is None or not message.text:
        return

    cfg: Config = ctx.application.bot_data["cfg"]
    if not cfg.is_allowed(chat.id):
        log.warning("chat non autorise : %s", chat.id)
        await message.reply_text(
            "Ce bot est privé 🙈 Demande à son propriétaire de t'ajouter "
            f"(identifiant de ce chat : {chat.id})."
        )
        return

    if not find_tiktok_url(message.text):
        await message.reply_text(HELP_FR, parse_mode=ParseMode.HTML)
        return

    status = await message.reply_text("🔎 Je récupère les photos…")
    ctx.application.create_task(
        run_job(update, ctx, message.text, status),
        update=update,
    )


async def run_job(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str, status) -> None:
    cfg: Config = ctx.application.bot_data["cfg"]
    deps: Deps = ctx.application.bot_data["deps"]
    locks: defaultdict[int, asyncio.Lock] = ctx.application.bot_data["locks"]
    semaphore: asyncio.Semaphore = ctx.application.bot_data["semaphore"]
    chat_id = update.effective_chat.id if update.effective_chat else 0

    async def progress(message: str) -> None:
        await status.edit_text(message)

    if locks[chat_id].locked():
        await status.edit_text("⏳ Je termine d'abord ta recherche précédente…")

    async with locks[chat_id], semaphore:
        try:
            results = await process_link(text, cfg, deps, progress)
        except (NotATikTokUrl, TikTokError, VintedError, LLMError, VisionError) as exc:
            user_message = getattr(exc, "user_message_fr", None) or "Ça n'a pas marché 😕"
            log.info("echec utilisateur (%s) : %s", type(exc).__name__, exc)
            await status.edit_text(user_message)
            return
        except Exception:
            log.exception("echec inattendu du pipeline")
            await status.edit_text("Oups, quelque chose a cassé de mon côté 😅 Réessaie plus tard.")
            return

        if not results:
            await status.edit_text("🤔 Je n'ai reconnu aucun vêtement sur ces photos.")
            return

        try:
            await status.delete()
        except Exception:
            log.debug("suppression du message de statut impossible", exc_info=True)

        for result in results:
            await send_garment_album(update.message, result, deps.http)


def build_caption(result: GarmentResult) -> str:
    """Legende HTML de l'album : une ligne par annonce, tronquee a 1024 caracteres."""
    header = f"🧵 <b>{html.escape(result.garment.label_fr)}</b>"
    if result.items:
        header += f" — {len(result.items)} annonce(s)"
    if result.note_fr:
        header += f"\n<i>{html.escape(result.note_fr)}</i>"

    lines = [header]
    for position, item in enumerate(result.items, start=1):
        title = html.escape(item.title[:40].strip())
        details = [item.price_label()]
        if item.brand_title:
            details.append(html.escape(item.brand_title))
        if item.size_title:
            details.append(html.escape(item.size_title))
        if item.status:
            details.append(html.escape(item.status))
        line = f'{position}. <a href="{html.escape(item.url, quote=True)}">{title}</a> — ' + " · ".join(
            details
        )
        if sum(len(part) + 1 for part in lines) + len(line) > CAPTION_LIMIT:
            break
        lines.append(line)
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

    caption = build_caption(result)
    if not result.items:
        await message.reply_text(
            f"{caption}\n\nRien trouvé sur Vinted 😕 Essaie avec des photos plus nettes, "
            "ou cherche à la main avec ces mots-clés.",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    items = [item for item in result.items if item.photo_url]
    if len(items) < 2:
        await _send_single(message, result.items[0], caption, http)
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
        await message.reply_text(caption, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


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
    await message.reply_text(caption, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def _with_retry(action):
    """Rejoue une action une fois si Telegram impose une pause (flood control)."""
    try:
        return await action()
    except RetryAfter as exc:
        await asyncio.sleep(float(exc.retry_after) + 0.5)
        return await action()


def main() -> None:
    setup_logging()
    try:
        cfg = load_config()
    except ConfigError as exc:
        raise SystemExit(f"Configuration invalide : {exc}") from exc

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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
