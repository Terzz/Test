#!/usr/bin/env bash
# Lance le bot. Ctrl+C pour l'arreter.
set -euo pipefail
cd "$(dirname "$0")"

[ -d .venv ] || { echo "Le projet n'est pas installé. Lance d'abord : ./install.sh" >&2; exit 1; }
[ -f .env ]  || { echo "Configuration manquante. Lance d'abord : ./install.sh" >&2; exit 1; }

echo "Bot fripe démarré. Envoie-lui un lien TikTok sur Telegram."
echo "Ctrl+C pour l'arrêter."
exec ./.venv/bin/python -m fripe.bot
