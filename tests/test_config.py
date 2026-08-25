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
