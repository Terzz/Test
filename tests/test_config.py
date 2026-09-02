"""Chargement de la configuration : les valeurs limites ne passent pas en douce."""

from __future__ import annotations

import pytest

from fripe.config import load_config


@pytest.fixture
def env_minimal(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:jeton-de-test")
    monkeypatch.setenv("LLM_BACKEND", "agent_sdk")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-test")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("MAX_RESULTS_PER_GARMENT", raising=False)


def test_max_results_zero_est_clampe_pas_ecrase(env_minimal, monkeypatch):
    # Un 0 explicite est une valeur hors bornes comme une autre : clampee a 2,
    # pas remplacee par le defaut 6 (piege du zero falsy).
    monkeypatch.setenv("MAX_RESULTS_PER_GARMENT", "0")
    assert load_config().max_results == 2


def test_max_results_defaut(env_minimal):
    assert load_config().max_results == 6


def test_journal_tournant_quand_un_fichier_est_demande(tmp_path, monkeypatch):
    from logging.handlers import RotatingFileHandler

    from fripe.config import log_handlers

    fichier = tmp_path / "logs" / "bot.log"
    monkeypatch.setenv("FRIPE_LOG_FILE", str(fichier))
    handlers = log_handlers()

    assert handlers is not None and isinstance(handlers[0], RotatingFileHandler)
    # Le dossier est cree : launchd ne le ferait pas a notre place.
    assert fichier.parent.is_dir()
    assert handlers[0].backupCount == 3
    handlers[0].close()


def test_console_par_defaut(monkeypatch):
    from fripe.config import log_handlers

    monkeypatch.delenv("FRIPE_LOG_FILE", raising=False)
    assert log_handlers() is None


def test_liste_vide_ferme_le_bot(env_minimal, monkeypatch):
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "")
    cfg = load_config()
    assert not cfg.open_to_all and not cfg.is_allowed(42)


def test_etoile_ouvre_le_bot_a_tous(env_minimal, monkeypatch):
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "*")
    cfg = load_config()
    assert cfg.open_to_all and cfg.is_allowed(42) and cfg.allowed_chat_ids == frozenset()


def test_liste_explicite(env_minimal, monkeypatch):
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "1, 2;3")
    cfg = load_config()
    assert cfg.is_allowed(2) and not cfg.is_allowed(4)


def test_les_jetons_quittent_l_environnement(env_minimal):
    import os

    cfg = load_config()

    # Config les garde ; les sous-processus (gallery-dl, CLI claude) n'en heritent plus.
    assert cfg.telegram_token == "123456:jeton-de-test"
    assert cfg.claude_oauth_token == "sk-ant-oat01-test"
    assert "TELEGRAM_BOT_TOKEN" not in os.environ
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ


def _avec_racine_propre(fonction):
    """Execute `fonction` avec une racine de logging vierge, puis rend celle de pytest."""
    import logging

    root = logging.getLogger()
    sauvegarde = root.handlers[:]
    niveau = root.level
    for handler in sauvegarde:
        root.removeHandler(handler)
    try:
        return fonction(root)
    finally:
        for handler in root.handlers[:]:
            root.removeHandler(handler)
            handler.close()
        for handler in sauvegarde:
            root.addHandler(handler)
        root.setLevel(niveau)


def test_mask_secrets_masque_les_jetons_meme_dans_les_traces():
    import io
    import logging

    from fripe.config import mask_secrets

    def scenario(root):
        tampon = io.StringIO()
        root.addHandler(logging.StreamHandler(tampon))
        root.setLevel(logging.INFO)
        mask_secrets(["123456:jeton-SECRET-XYZ", None, "court"])
        logger = logging.getLogger("fripe.test")
        try:
            raise RuntimeError("The token `123456:jeton-SECRET-XYZ` was rejected")
        except RuntimeError:
            logger.exception("jeton 123456:jeton-SECRET-XYZ refuse")
        return tampon.getvalue()

    sortie = _avec_racine_propre(scenario)
    assert "SECRET" not in sortie
    assert sortie.count("***") >= 2


def test_setup_logging_ecrit_dans_le_fichier_avec_la_date(monkeypatch, tmp_path):
    """Contrat avec autostart.sh : la ligne « pret (backend » atterrit dans FRIPE_LOG_FILE."""
    import logging

    from fripe.config import setup_logging

    fichier = tmp_path / "logs" / "bot.log"
    monkeypatch.setenv("FRIPE_LOG_FILE", str(fichier))
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    def scenario(root):
        setup_logging()
        logging.getLogger("fripe.bot").info("bot @%s pret (backend=%s)", "x", "agent_sdk")
        for handler in root.handlers:
            handler.flush()
        return fichier.read_text(encoding="utf-8")

    contenu = _avec_racine_propre(scenario)
    assert "pret (backend=agent_sdk)" in contenu
    assert contenu[:4].isdigit() and contenu[4] == "-"
    assert oct(fichier.stat().st_mode & 0o777) == "0o600"


def test_setup_logging_lit_le_env_avant_de_choisir_le_niveau(monkeypatch):
    import logging

    from fripe import config

    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("FRIPE_LOG_FILE", raising=False)
    # Simule un .env qui demande DEBUG : il doit etre lu avant basicConfig.
    monkeypatch.setattr(config, "load_dotenv", lambda: monkeypatch.setenv("LOG_LEVEL", "DEBUG"))

    def scenario(root):
        config.setup_logging()
        return root.level

    assert _avec_racine_propre(scenario) == logging.DEBUG
