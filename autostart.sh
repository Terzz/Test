#!/usr/bin/env bash
# Demarrage automatique du bot au login du Mac, via launchd.
#
#   ./autostart.sh            installe et demarre (relancable sans risque)
#   ./autostart.sh status     le bot tourne-t-il ? dernieres lignes du journal
#   ./autostart.sh logs       suit le journal en direct (Ctrl+C pour sortir)
#   ./autostart.sh restart    relance le bot (apres un `git pull` par exemple)
#   ./autostart.sh off        desinstalle le demarrage automatique
#
# Le bot est relance s'il plante et rattrape, a chaque demarrage, les liens
# envoyes pendant qu'il etait eteint (Telegram les garde 24 h).
# Sur Linux / Raspberry Pi, voir deploy/fripe.service.

set -euo pipefail
cd "$(dirname "$0")"
REPO="$(pwd -P)"

LABEL=com.fripe.bot
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAINE="gui/$(id -u)"
JOURNAL="$REPO/data/logs/bot.log"
LANCEMENT="$REPO/data/logs/launchd.log"

xml() { printf '%s' "$1" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g'; }

plist() {
    cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$(xml "$REPO")/.venv/bin/python</string>
        <string>-m</string>
        <string>fripe.bot</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$(xml "$REPO")</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>FRIPE_LOG_FILE</key>
        <string>$(xml "$JOURNAL")</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>StandardOutPath</key>
    <string>$(xml "$LANCEMENT")</string>
    <key>StandardErrorPath</key>
    <string>$(xml "$LANCEMENT")</string>
</dict>
</plist>
EOF
}

mac_requis() {
    [ "$(uname -s)" = Darwin ] && return
    echo "Ce script est pour macOS. Sur Linux / Raspberry Pi : deploy/fripe.service" >&2
    exit 1
}

est_charge() { launchctl print "$DOMAINE/$LABEL" >/dev/null 2>&1; }

pid_du_bot() {
    launchctl print "$DOMAINE/$LABEL" 2>/dev/null \
        | sed -n 's/^[[:space:]]*pid = \([0-9][0-9]*\).*/\1/p' | head -1
}

# Un bot lance a la main en parallele ferait doublon : Telegram refuse deux
# ecoutes simultanees (409) et aucun des deux ne recevrait tout.
bot_manuel() { pgrep -f '[.]venv/bin/python -m fripe\.bot' 2>/dev/null || true; }

lignes_journal() { if [ -f "$JOURNAL" ]; then wc -l < "$JOURNAL" | tr -d ' '; else echo 0; fi; }

attendre_demarrage() {
    # Attend jusqu'a 20 s que le journal annonce le bot pret, ou une erreur.
    local avant="$1" i nouvelles
    for i in $(seq 1 20); do
        sleep 1
        nouvelles="$(tail -n +"$((avant + 1))" "$JOURNAL" 2>/dev/null || true)"
        if printf '%s' "$nouvelles" | grep -q 'pret (backend'; then
            echo "  [OK]    $(printf '%s' "$nouvelles" | grep 'pret (backend' | tail -1 | sed 's/.*| //')"
            return 0
        fi
        if printf '%s' "$nouvelles" | grep -Eq 'ERROR|Traceback' \
            || tail -c 3000 "$LANCEMENT" 2>/dev/null | grep -Eq 'Traceback|Error'; then
            echo "  [ECHEC] le bot n'a pas pu demarrer. Dernieres lignes :"
            { printf '%s\n' "$nouvelles"; tail -n 6 "$LANCEMENT" 2>/dev/null; } \
                | grep -Ev '^[[:space:]]*File |^[[:space:]]*$' | tail -n 6 | sed 's/^/          /'
            return 1
        fi
    done
    if printf '%s' "$nouvelles" | grep -q 'connexion à Telegram'; then
        echo "  [INFO]  le bot attend Telegram (pas de réseau ?). Il réessaie tout seul ; ./autostart.sh status pour suivre."
    else
        echo "  [INFO]  pas encore de signe de vie après 20 s — regarde ./autostart.sh status dans un instant."
    fi
    return 0
}

installer() {
    mac_requis
    [ -x .venv/bin/python ] || { echo "Le projet n'est pas installé. Lance d'abord : ./install.sh" >&2; exit 1; }
    [ -f .env ] || { echo "Configuration manquante. Lance d'abord : ./install.sh" >&2; exit 1; }

    case "$REPO" in
        "$HOME"/Desktop/*|"$HOME"/Documents/*|"$HOME"/Downloads/*|"$HOME"/Library/Mobile\ Documents/*)
            echo "⚠️  Le projet est dans un dossier que macOS protège (Bureau, Documents, Téléchargements, iCloud)."
            echo "    Les programmes en arrière-plan n'y ont pas toujours accès. Si le bot ne démarre pas,"
            echo "    déplace le projet à la racine de ton dossier personnel, par exemple ~/fripe."
            echo ;;
    esac

    local pid_manuel
    pid_manuel="$(bot_manuel)"
    if [ -n "$pid_manuel" ] && ! est_charge; then
        echo "Un bot lancé à la main tourne déjà (pid $pid_manuel)."
        printf "L'arrêter pour le passer en automatique ? [O/n] "
        read -r reponse
        case "$reponse" in
            n|N|non|Non) echo "Rien n'a été changé. Arrête-le (Ctrl+C) puis relance ./autostart.sh"; exit 1 ;;
        esac
        kill "$pid_manuel" 2>/dev/null || true
        sleep 2
    fi

    mkdir -p data/logs "$HOME/Library/LaunchAgents"
    : > "$LANCEMENT"
    plist > "$PLIST"

    # bootstrap refuse un job deja charge : on decharge d'abord (relance propre).
    if est_charge; then
        launchctl bootout "$DOMAINE/$LABEL" 2>/dev/null || true
        sleep 1
    fi
    launchctl enable "$DOMAINE/$LABEL" 2>/dev/null || true
    local avant
    avant="$(lignes_journal)"
    launchctl bootstrap "$DOMAINE" "$PLIST"

    echo "Démarrage automatique installé ($PLIST)."
    echo "Le bot démarre maintenant, puis à chaque ouverture de session…"
    attendre_demarrage "$avant" || true
    echo
    echo "Et maintenant :"
    echo "  ./autostart.sh status    l'état et les dernières lignes du journal"
    echo "  ./autostart.sh logs      le journal en direct"
    echo "  ./autostart.sh restart   après un git pull"
    echo "  ./autostart.sh off       pour tout retirer"
    echo
    echo "Les liens envoyés pendant que le Mac est éteint sont traités au réveil (Telegram les garde 24 h)."
}

statut() {
    mac_requis
    if [ ! -f "$PLIST" ]; then
        echo "Démarrage automatique : non installé. Pour l'installer : ./autostart.sh"
        exit 1
    fi
    if ! est_charge; then
        echo "Démarrage automatique : installé mais pas chargé. Relance ./autostart.sh"
        exit 1
    fi
    local pid
    pid="$(pid_du_bot)"
    if [ -n "$pid" ]; then
        echo "Le bot tourne (pid $pid) et démarre automatiquement au login."
    else
        echo "Le bot est chargé mais ne tourne pas en ce moment (relance automatique en cours ?)."
    fi
    echo
    echo "Dernières lignes du journal ($JOURNAL) :"
    tail -n 15 "$JOURNAL" 2>/dev/null || echo "(journal vide)"
    if [ -s "$LANCEMENT" ]; then
        echo
        echo "Sorties de lancement ($LANCEMENT) :"
        tail -n 8 "$LANCEMENT"
    fi
}

journal() {
    mac_requis
    [ -f "$JOURNAL" ] || { echo "Pas encore de journal ($JOURNAL). Le bot a-t-il démarré ? ./autostart.sh status"; exit 1; }
    echo "Journal en direct — Ctrl+C pour sortir."
    exec tail -n 30 -f "$JOURNAL"
}

relancer() {
    mac_requis
    est_charge || { echo "Le démarrage automatique n'est pas installé. Lance ./autostart.sh"; exit 1; }
    local avant
    avant="$(lignes_journal)"
    : > "$LANCEMENT"
    launchctl kickstart -k "$DOMAINE/$LABEL"
    echo "Bot relancé…"
    attendre_demarrage "$avant" || true
}

desinstaller() {
    mac_requis
    if est_charge; then launchctl bootout "$DOMAINE/$LABEL" 2>/dev/null || true; fi
    rm -f "$PLIST"
    echo "Démarrage automatique retiré : le bot ne tourne plus."
    echo "Pour le lancer à la main : ./start.sh"
}

case "${1:-install}" in
    install|on)   installer ;;
    status|etat)  statut ;;
    logs|log)     journal ;;
    restart)      relancer ;;
    off|uninstall|remove) desinstaller ;;
    plist)        plist ;;
    *)  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
