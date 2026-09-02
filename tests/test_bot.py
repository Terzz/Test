"""Bot Telegram : mise en forme des reponses, handlers, livraison des albums, demarrage."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from telegram import Bot, InputFile, Update
from telegram.error import BadRequest, Conflict, InvalidToken, NetworkError, RetryAfter
from telegram.ext import Application

import fripe.bot as botmod
from fripe.bot import (
    BAD_LINK_FR,
    CAPTION_LIMIT,
    DUPLICATE_FR,
    HINT_FR,
    NO_GARMENT_FR,
    RESTARTING_FR,
    ack_text,
    build_caption,
    no_result_text,
    run_job,
    send_garment_album,
)
from fripe.config import Config, ConfigError
from fripe.models import Garment, GarmentResult, VintedItem
from fripe.tiktok import TikTokError
from fripe.vinted import VintedError
from tests.conftest import make_jpeg

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
LIEN = "https://vm.tiktok.com/ZMabc123/"


def make_item(item_id: int, title: str = "Veste en cuir marron vintage", photo: bool = True) -> VintedItem:
    return VintedItem(
        id=item_id,
        title=title,
        url=f"https://www.vinted.fr/items/{item_id}",
        price_amount=25.0,
        brand_title="Zara",
        size_title="M",
        status="Très bon état",
        photo_url=f"https://images1.vinted.net/{item_id}.jpeg" if photo else None,
    )


def make_result(items, note=None, queries=("veste cuir marron", "blouson cuir marron")) -> GarmentResult:
    garment = Garment(
        id="g1", label_fr="veste en cuir marron", queries_fr=list(queries), catalog_id=1908, color_ids=[2]
    )
    return GarmentResult(garment=garment, items=items, note_fr=note)


def make_config(tmp_path: Path, **overrides) -> Config:
    defaults = dict(
        telegram_token="123456:jeton-de-test",
        llm_backend="agent_sdk",
        claude_oauth_token="sk-ant-oat01-test",
        anthropic_api_key=None,
        analysis_model="claude-opus-5",
        rerank_model="claude-haiku-4-5",
        reranker="claude",
        data_dir=tmp_path,
        max_results=6,
        price_to=None,
        allowed_chat_ids=frozenset({42}),
    )
    defaults.update(overrides)
    return Config(**defaults)


def make_update(text: str, date: datetime = NOW, chat_id: int = 42, *, caption: bool = False):
    """Vrai objet Update (de_json) attache a un faux Bot : reply_text -> send_message."""
    bot = MagicMock(spec=Bot)
    bot.defaults = None
    bot.send_message = AsyncMock(return_value="statut")
    message = {
        "message_id": 7,
        "date": int(date.timestamp()),
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": chat_id, "is_bot": False, "first_name": "T"},
    }
    message["caption" if caption else "text"] = text
    update = Update.de_json({"update_id": 1, "message": message}, bot)
    return update, bot.send_message


def make_ctx(cfg: Config, *, running: bool = True):
    ctx = MagicMock()
    ctx.application.bot_data = {"cfg": cfg, "recents": {}}
    ctx.application.running = running
    # Ferme la coroutine recue : on ne l'execute pas, et sans ca Python avertit.
    ctx.application.create_task = MagicMock(side_effect=lambda coro, **_: coro.close())
    return ctx


def texte_envoye(send: AsyncMock, index: int = -1) -> str:
    return send.call_args_list[index].kwargs["text"]


# ── accuse de reception ───────────────────────────────────────────────────────


def test_ack_signale_un_lien_recu_pendant_l_arret():
    assert "éteint" in ack_text(NOW - timedelta(hours=3), NOW)


def test_ack_normal_pour_un_lien_frais():
    assert "éteint" not in ack_text(NOW - timedelta(seconds=5), NOW)
    assert "éteint" not in ack_text(None, NOW)


def test_ack_annonce_plusieurs_liens():
    assert "3 liens" in ack_text(NOW, NOW, count=3)


def test_le_bot_rattrape_les_messages_en_attente():
    # Sans ces deux options, les liens envoyes pendant l'arret seraient jetes
    # au demarrage, et une machine sans reseau au reveil ferait planter le bot.
    assert botmod.POLLING_OPTIONS["drop_pending_updates"] is False
    assert botmod.POLLING_OPTIONS["bootstrap_retries"] < 0


# ── on_message ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("age,attendu", [(timedelta(hours=3), True), (timedelta(seconds=5), False)])
async def test_on_message_accuse_selon_la_date_du_message(tmp_path, age, attendu):
    ctx = make_ctx(make_config(tmp_path))
    update, send = make_update(LIEN, datetime.now(timezone.utc) - age)

    await botmod.on_message(update, ctx)

    assert ("éteint" in texte_envoye(send)) is attendu
    assert ctx.application.create_task.call_count == 1


async def test_on_message_plusieurs_liens_un_seul_accuse(tmp_path):
    update, send = make_update(f"{LIEN} et https://vm.tiktok.com/ZMdef456/")
    ctx = make_ctx(make_config(tmp_path))

    await botmod.on_message(update, ctx)

    assert send.call_count == 1
    assert "2 liens" in texte_envoye(send)
    assert ctx.application.create_task.call_count == 2


async def test_on_message_ignore_un_lien_deja_en_cours(tmp_path):
    ctx = make_ctx(make_config(tmp_path))
    update, send = make_update(LIEN)
    await botmod.on_message(update, ctx)
    update, send = make_update(LIEN)
    await botmod.on_message(update, ctx)

    assert texte_envoye(send) == DUPLICATE_FR
    assert ctx.application.create_task.call_count == 1


async def test_on_message_lit_la_legende_d_une_photo(tmp_path):
    update, send = make_update(LIEN, caption=True)
    ctx = make_ctx(make_config(tmp_path))
    await botmod.on_message(update, ctx)
    assert ctx.application.create_task.call_count == 1


async def test_on_message_sans_lien_repond_court(tmp_path):
    ctx = make_ctx(make_config(tmp_path))
    update, send = make_update("merci !")
    await botmod.on_message(update, ctx)
    assert texte_envoye(send) == HINT_FR

    update, send = make_update("tiens un tiktok : https://www.tiktok.com/discover/mode")
    await botmod.on_message(update, ctx)
    assert texte_envoye(send) == BAD_LINK_FR
    assert not ctx.application.create_task.called


async def test_on_message_refuse_tant_que_la_liste_est_vide(tmp_path):
    ctx = make_ctx(make_config(tmp_path, allowed_chat_ids=frozenset()))
    update, send = make_update(LIEN, chat_id=777)
    await botmod.on_message(update, ctx)

    assert "777" in texte_envoye(send) and "ALLOWED_CHAT_IDS" in texte_envoye(send)
    assert not ctx.application.create_task.called


async def test_on_message_refuse_un_inconnu(tmp_path):
    ctx = make_ctx(make_config(tmp_path, allowed_chat_ids=frozenset({1})))
    update, send = make_update(LIEN, chat_id=42)
    await botmod.on_message(update, ctx)
    assert "privé" in texte_envoye(send) and "42" in texte_envoye(send)


async def test_on_message_ouvert_a_tous_avec_etoile(tmp_path):
    ctx = make_ctx(make_config(tmp_path, allowed_chat_ids=frozenset(), open_to_all=True))
    update, send = make_update(LIEN, chat_id=999)
    await botmod.on_message(update, ctx)
    assert ctx.application.create_task.call_count == 1


async def test_on_message_pendant_un_arret(tmp_path):
    ctx = make_ctx(make_config(tmp_path), running=False)
    update, send = make_update(LIEN)
    await botmod.on_message(update, ctx)
    assert texte_envoye(send) == RESTARTING_FR
    assert not ctx.application.create_task.called


async def test_on_message_survit_a_un_accuse_refuse(tmp_path):
    # Un 429 sur l'accuse ne doit pas faire perdre le lien (deja acquitte).
    update, send = make_update(LIEN)
    send.side_effect = RetryAfter(1)
    ctx = make_ctx(make_config(tmp_path))
    asyncio_sleep = AsyncMock()
    botmod_sleep = botmod.asyncio.sleep
    botmod.asyncio.sleep = asyncio_sleep
    try:
        await botmod.on_message(update, ctx)
    finally:
        botmod.asyncio.sleep = botmod_sleep
    assert ctx.application.create_task.call_count == 1


# ── run_job ───────────────────────────────────────────────────────────────────


def make_job_ctx(cfg: Config):
    ctx = MagicMock()
    ctx.application.bot_data = {
        "cfg": cfg,
        "deps": MagicMock(),
        "locks": defaultdict(asyncio.Lock),
        "semaphore": asyncio.Semaphore(2),
        "recents": {},
    }
    return ctx


def make_status():
    status = MagicMock()
    status.edit_text = AsyncMock()
    status.delete = AsyncMock()
    return status


async def test_run_job_transmet_le_message_d_erreur(tmp_path, monkeypatch):
    async def echec(*_a, **_k):
        raise VintedError("403", user_message_fr="Vinted bloque")

    monkeypatch.setattr(botmod, "process_link", echec)
    update, _ = make_update(LIEN)
    status = make_status()

    await run_job(update, make_job_ctx(make_config(tmp_path)), LIEN, status)

    assert status.edit_text.call_args.args[0] == "Vinted bloque"


async def test_run_job_recommence_une_fois_apres_une_veille(tmp_path, monkeypatch):
    appels = []

    async def parfois(*_a, **_k):
        appels.append(1)
        if len(appels) == 1:
            raise TikTokError("connexion morte")
        return []

    monkeypatch.setattr(botmod, "process_link", parfois)
    # Sans vraie veille, on force la detection : tout echec compte comme « a dormi ».
    monkeypatch.setattr(botmod, "SLEEP_GAP_S", -1)
    update, _ = make_update(LIEN)
    status = make_status()

    await run_job(update, make_job_ctx(make_config(tmp_path)), LIEN, status)

    textes = [c.args[0] for c in status.edit_text.call_args_list]
    assert len(appels) == 2
    assert any(t.startswith("💤") for t in textes)
    assert textes[-1] == NO_GARMENT_FR


async def test_run_job_sans_statut_ne_plante_pas(tmp_path, monkeypatch):
    async def vide(*_a, **_k):
        return []

    monkeypatch.setattr(botmod, "process_link", vide)
    update, _ = make_update(LIEN)
    await run_job(update, make_job_ctx(make_config(tmp_path)), LIEN, None)


async def test_run_job_ignore_une_edition_identique(tmp_path, monkeypatch):
    async def vide(*_a, **_k):
        return []

    monkeypatch.setattr(botmod, "process_link", vide)
    status = make_status()
    status.edit_text.side_effect = BadRequest("Message is not modified")
    update, _ = make_update(LIEN)
    await run_job(update, make_job_ctx(make_config(tmp_path)), LIEN, status)


async def test_run_job_envoie_les_albums_puis_retire_le_statut(tmp_path, monkeypatch):
    async def resultats(*_a, **_k):
        return [make_result([make_item(1), make_item(2)])]

    envoyes = []

    async def faux_envoi(message, result, http):
        envoyes.append(result)

    monkeypatch.setattr(botmod, "process_link", resultats)
    monkeypatch.setattr(botmod, "send_garment_album", faux_envoi)
    status = make_status()
    update, _ = make_update(LIEN)

    await run_job(update, make_job_ctx(make_config(tmp_path)), LIEN, status)

    assert len(envoyes) == 1
    assert status.delete.await_count == 1


# ── legendes ──────────────────────────────────────────────────────────────────


def test_caption_liste_les_annonces():
    caption = build_caption(make_result([make_item(1), make_item(2)]))

    assert "veste en cuir marron" in caption
    assert '<a href="https://www.vinted.fr/items/1">' in caption
    assert "25€" in caption
    assert "Zara" in caption
    assert "Très bon état" in caption


def test_caption_affiche_la_note_d_elargissement():
    caption = build_caption(make_result([make_item(1)], note="recherche élargie : couleur ignorée"))
    assert "<i>recherche élargie : couleur ignorée</i>" in caption


def test_caption_echappe_le_html():
    mechant = make_item(1, title="Veste <b>choc</b> & compagnie")
    caption = build_caption(make_result([mechant]))

    assert "<b>choc</b>" not in caption
    assert "&lt;b&gt;choc" in caption
    assert "&amp; compagnie" in caption


def test_caption_reste_sous_la_limite_telegram():
    items = [make_item(i, title="Veste en cuir marron vintage taille M" * 2) for i in range(1, 11)]
    caption = build_caption(make_result(items, note="recherche élargie : filtres ignorés"))

    assert len(caption) <= CAPTION_LIMIT
    # La coupe n'est plus silencieuse.
    assert "… et " in caption


def test_caption_termine_par_un_lien_vinted():
    caption = build_caption(make_result([make_item(1)]))
    assert "Voir plus sur Vinted" in caption
    assert "search_text=veste+cuir+marron" in caption
    assert "catalog%5B%5D=1908" in caption


def test_caption_numerote_dans_l_ordre_des_photos():
    # L'annonce 2 n'a pas de vignette : elle passe en fin, marquee, pour que
    # « 2. » designe bien la deuxieme photo de l'album.
    caption = build_caption(make_result([make_item(1), make_item(2, photo=False), make_item(3)]))
    lignes = caption.splitlines()
    assert lignes[2].startswith('2. <a href="https://www.vinted.fr/items/3">')
    assert "sans photo" in lignes[3]


def test_caption_accorde_le_pluriel():
    une = build_caption(make_result([make_item(1)]))
    plusieurs = build_caption(make_result([make_item(1), make_item(2)]))
    assert "1 annonce" in une and "1 annonces" not in une
    assert "2 annonces" in plusieurs


def test_rien_trouve_donne_les_recherches_cliquables():
    texte = no_result_text(make_result([]))
    assert "veste en cuir marron" in texte
    assert texte.count("vinted.fr/catalog?search_text=") == 2
    assert "blouson+cuir+marron" in texte


# ── envoi des albums ──────────────────────────────────────────────────────────


def make_message():
    message = MagicMock()
    message.reply_media_group = AsyncMock()
    message.reply_photo = AsyncMock()
    message.reply_text = AsyncMock()
    return message


def make_http() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=make_jpeg())))


async def test_album_retombe_sur_les_octets_si_une_url_est_refusee():
    message = make_message()
    message.reply_media_group.side_effect = [BadRequest("wrong file"), None]

    await send_garment_album(message, make_result([make_item(1), make_item(2)]), make_http())

    assert message.reply_media_group.await_count == 2
    medias = message.reply_media_group.call_args.args[0]
    # PTB enveloppe les octets dans un InputFile ; une URL resterait une chaine.
    assert all(isinstance(m.media, InputFile) for m in medias)
    assert medias[0].caption and medias[1].caption is None


async def test_album_photo_par_photo_en_dernier_recours():
    message = make_message()
    message.reply_media_group.side_effect = BadRequest("wrong file")

    await send_garment_album(message, make_result([make_item(1), make_item(2), make_item(3)]), make_http())

    assert message.reply_photo.await_count == 3
    legendes = [c.kwargs["caption"] for c in message.reply_photo.call_args_list]
    assert legendes[0] and legendes[1] is None and legendes[2] is None


async def test_album_sans_annonce_envoie_les_recherches():
    message = make_message()
    await send_garment_album(message, make_result([]), make_http())
    assert "vinted.fr/catalog" in message.reply_text.call_args.args[0]


async def test_with_retry_attend_le_delai_impose(monkeypatch):
    pauses = []

    async def faux_sleep(delai):
        pauses.append(delai)

    monkeypatch.setattr(botmod.asyncio, "sleep", faux_sleep)
    action = AsyncMock(side_effect=[RetryAfter(2), "ok"])

    assert await botmod._with_retry(action) == "ok"
    assert pauses == [2.5]


# ── demarrage et erreurs ──────────────────────────────────────────────────────


@pytest.fixture
def env_bot(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:jeton-de-test-ABCDEFGHIJKLMNOPQRSTUVWX")
    monkeypatch.setenv("LLM_BACKEND", "agent_sdk")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-test")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "42")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Le journal est teste a part : sous pytest, le reconfigurer casserait caplog.
    monkeypatch.setattr(botmod, "setup_logging", lambda: None)
    monkeypatch.setattr(botmod, "mask_secrets", lambda secrets: None)


def test_main_passe_les_options_de_rattrapage_a_run_polling(env_bot, monkeypatch, caplog):
    recu = {}

    def faux_run_polling(self, **kwargs):
        recu.update(kwargs)

    monkeypatch.setattr(Application, "run_polling", faux_run_polling)
    with caplog.at_level(logging.INFO, logger="fripe.bot"):
        botmod.main()

    assert recu["drop_pending_updates"] is False and recu["bootstrap_retries"] < 0
    # Le marqueur que autostart.sh guette dans le journal.
    assert "connexion à Telegram" in caplog.text


def test_main_jeton_refuse_sort_sans_le_recopier(env_bot, monkeypatch, caplog):
    def faux_run_polling(self, **kwargs):
        raise InvalidToken("The token `123456:jeton-de-test-ABCDEFGHIJKLMNOPQRSTUVWX` was rejected")

    monkeypatch.setattr(Application, "run_polling", faux_run_polling)
    with caplog.at_level(logging.ERROR, logger="fripe.bot"), pytest.raises(SystemExit) as exc:
        botmod.main()

    assert exc.value.code == botmod.EXIT_DEFINITIVE
    assert "TELEGRAM_BOT_TOKEN" in caplog.text and "install.sh" in caplog.text
    assert "ABCDEFGHIJKLMNOPQRSTUVWX" not in caplog.text


def test_main_config_invalide_va_dans_le_journal(env_bot, monkeypatch, caplog):
    monkeypatch.setattr(botmod, "load_config", MagicMock(side_effect=ConfigError("TELEGRAM_BOT_TOKEN est vide")))
    with caplog.at_level(logging.ERROR, logger="fripe.bot"), pytest.raises(SystemExit) as exc:
        botmod.main()
    assert exc.value.code == botmod.EXIT_DEFINITIVE and "Configuration invalide" in caplog.text


async def test_on_error_resume_les_coupures_reseau(caplog):
    ctx = MagicMock()
    ctx.application.bot_data = {"net_error_at": 0.0}
    ctx.error = NetworkError("httpx.ConnectError")
    with caplog.at_level(logging.WARNING, logger="fripe.bot"):
        await botmod.on_error(None, ctx)
        await botmod.on_error(None, ctx)

    assert caplog.text.count("Telegram injoignable") == 1
    assert "Traceback" not in caplog.text


async def test_on_error_nomme_le_doublon_de_bot(caplog):
    ctx = MagicMock()
    ctx.application.bot_data = {"net_error_at": 0.0}
    ctx.error = Conflict("terminated by other getUpdates request")
    with caplog.at_level(logging.WARNING, logger="fripe.bot"):
        await botmod.on_error(None, ctx)
    assert "409" in caplog.text and "start.sh" in caplog.text


async def test_on_error_trace_le_reste(caplog):
    ctx = MagicMock()
    ctx.application.bot_data = {}
    ctx.error = RuntimeError("boum")
    with caplog.at_level(logging.ERROR, logger="fripe.bot"):
        await botmod.on_error(object(), ctx)
    assert "boum" in caplog.text


def test_trim_launchd_log_borne_le_fichier(tmp_path, monkeypatch):
    journal = tmp_path / "bot.log"
    launchd = tmp_path / "launchd.log"
    launchd.write_bytes(b"x" * (botmod.LAUNCHD_LOG_MAX + 10))
    monkeypatch.setenv("FRIPE_LOG_FILE", str(journal))

    botmod._trim_launchd_log()

    assert launchd.stat().st_size == botmod.LAUNCHD_LOG_KEEP


def test_dedup_oublie_les_vieux_liens():
    recents = {(42, LIEN): time.monotonic() - botmod.DEDUP_WINDOW_S - 1}
    assert botmod._seen_recently(recents, 42, LIEN) is False
    assert recents == {}
