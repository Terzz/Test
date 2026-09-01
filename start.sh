#!/usr/bin/env bash
# Lance le bot. Ctrl+C pour l'arreter.
set -euo pipefail
cd "$(dirname "$0")"

[ -d .venv ] || { echo "Le projet n'est pas installé. Lance d'abord : ./install.sh" >&2; exit 1; }
[ -f .env ]  || { echo "Configuration manquante. Lance d'abord : ./install.sh" >&2; exit 1; }

# Deux bots a l'ecoute du meme jeton se volent les messages (Telegram repond 409).
if [ "$(uname -s)" = Darwin ] && launchctl print "gui/$(id -u)/com.fripe.bot" >/dev/null 2>&1; then
    echo "Le bot tourne déjà en arrière-plan (démarrage automatique)." >&2
    echo "Journal : ./autostart.sh logs   —   pour le lancer à la main : ./autostart.sh off, puis ./start.sh" >&2
    exit 1
fi

echo "Bot fripe démarré. Envoie-lui un lien TikTok sur Telegram."
echo "Ctrl+C pour l'arrêter."
exec ./.venv/bin/python -m fripe.bot
