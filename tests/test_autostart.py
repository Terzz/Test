"""autostart.sh : le plist genere et la lecture du journal, testables sans launchd."""

from __future__ import annotations

import plistlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "autostart.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash absent")


def plist_de(script: Path) -> dict:
    sortie = subprocess.run(["bash", str(script), "plist"], capture_output=True, check=True)
    return plistlib.loads(sortie.stdout)


def test_plist_valide_et_coherent():
    plist = plist_de(SCRIPT)

    assert plist["Label"] == "com.fripe.bot"
    assert plist["ProgramArguments"] == [str(REPO / ".venv/bin/python"), "-m", "fripe.bot"]
    assert plist["WorkingDirectory"] == str(REPO)
    assert plist["EnvironmentVariables"]["FRIPE_LOG_FILE"] == str(REPO / "data/logs/bot.log")
    assert ".local/bin" in plist["EnvironmentVariables"]["PATH"]
    assert plist["RunAtLoad"] is True
    # Relance sur plantage seulement : un arret volontaire (code 0) ne boucle pas.
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    # Le temps de finir une recherche avant que launchd ne tue le bot.
    assert plist["ExitTimeOut"] >= 180
    assert plist["StandardOutPath"] == plist["StandardErrorPath"]


def test_plist_survit_a_un_chemin_hostile(tmp_path):
    dossier = tmp_path / "Mon dossier & <co> 'été'"
    dossier.mkdir()
    copie = dossier / "autostart.sh"
    copie.write_bytes(SCRIPT.read_bytes())
    copie.chmod(0o755)

    plist = plist_de(copie)

    assert plist["WorkingDirectory"] == str(dossier)
    assert plist["ProgramArguments"][0] == str(dossier / ".venv/bin/python")


def test_le_label_est_le_meme_partout():
    for nom in ("start.sh", "diagnostic.sh"):
        assert "com.fripe.bot" in (REPO / nom).read_text(encoding="utf-8"), nom


def test_les_marqueurs_du_journal_existent_dans_le_bot():
    """autostart.sh guette deux phrases dans bot.log : elles doivent exister telles quelles."""
    script = SCRIPT.read_text(encoding="utf-8")
    bot = (REPO / "fripe/bot.py").read_text(encoding="utf-8")
    for marqueur in ("pret (backend", "connexion à Telegram"):
        assert marqueur in script and marqueur in bot, marqueur


def attendre(tmp_path: Path, journal: str, lancement: str = "", avant: int = 0) -> tuple[int, str]:
    (tmp_path / "bot.log").write_text(journal, encoding="utf-8")
    (tmp_path / "launchd.log").write_text(lancement, encoding="utf-8")
    commande = (
        f"source '{SCRIPT}'; JOURNAL='{tmp_path}/bot.log'; LANCEMENT='{tmp_path}/launchd.log'; "
        f"attendre_demarrage {avant}"
    )
    sortie = subprocess.run(
        ["bash", "-c", commande], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "FRIPE_ATTENTE_PAS": "0", "HOME": str(tmp_path)}
    )
    return sortie.returncode, sortie.stdout


def test_attendre_demarrage_reconnait_le_bot_pret(tmp_path):
    code, sortie = attendre(
        tmp_path,
        "2026-09-01 10:00:00 INFO    fripe.bot | connexion à Telegram…\n"
        "2026-09-01 10:00:01 INFO    fripe.bot | bot @fripe pret (backend=agent_sdk)\n",
    )
    assert code == 0 and "[OK]" in sortie and "bot @fripe pret" in sortie


def test_attendre_demarrage_montre_l_erreur(tmp_path):
    code, sortie = attendre(
        tmp_path,
        "2026-09-01 10:00:01 ERROR   fripe.bot | Telegram refuse le jeton TELEGRAM_BOT_TOKEN : vérifie le fichier .env\n",
    )
    assert code == 1 and "[ECHEC]" in sortie and "TELEGRAM_BOT_TOKEN" in sortie


def test_attendre_demarrage_sans_reseau(tmp_path):
    code, sortie = attendre(tmp_path, "2026-09-01 10:00:00 INFO    fripe.bot | connexion à Telegram…\n")
    assert code == 0 and "attend Telegram" in sortie


def test_attendre_demarrage_ignore_les_vieilles_lignes(tmp_path):
    code, sortie = attendre(
        tmp_path,
        "vieille ERROR\nvieille ERROR\n2026-09-01 10:00:01 INFO fripe.bot | bot @x pret (backend=api)\n",
        avant=2,
    )
    assert code == 0 and "[OK]" in sortie


def test_attendre_demarrage_lit_le_plantage_launchd(tmp_path):
    code, sortie = attendre(
        tmp_path,
        "",
        lancement="Traceback (most recent call last):\n  File \"x\"\nModuleNotFoundError: No module named 'fripe'\n",
    )
    assert code == 1 and "ModuleNotFoundError" in sortie and 'File "x"' not in sortie


def test_le_script_peut_etre_source_sans_rien_lancer(tmp_path):
    sortie = subprocess.run(
        ["bash", "-c", f"source '{SCRIPT}'; echo source-ok"],
        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert sortie.returncode == 0 and sortie.stdout.strip() == "source-ok"
