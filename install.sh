#!/usr/bin/env bash
# Installation guidee de fripe. Relancable sans risque : rien n'est ecrase sans demander.
set -euo pipefail

cd "$(dirname "$0")"

BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; OFF=$'\033[0m'
if [ ! -t 1 ]; then BOLD=""; DIM=""; GREEN=""; YELLOW=""; RED=""; OFF=""; fi

titre()   { printf '\n%s\n%s\n' "${BOLD}$1${OFF}" "${DIM}$(printf '─%.0s' $(seq 1 ${#1}))${OFF}"; }
ok()      { printf '%s✓%s %s\n' "$GREEN" "$OFF" "$1"; }
info()    { printf '  %s\n' "$1"; }
alerte()  { printf '%s!%s %s\n' "$YELLOW" "$OFF" "$1"; }
echec()   { printf '%s✗%s %s\n' "$RED" "$OFF" "$1" >&2; exit 1; }

# Demande une valeur en gardant celle qui existe deja dans .env.
demander() {
    local cle="$1" question="$2" motif="${3:-}" conseil="${4:-}" actuelle reponse essai
    actuelle="$(valeur_env "$cle")"
    if [ -n "$actuelle" ]; then
        printf '  %s est déjà renseigné. Le remplacer ? [o/N] ' "$cle"
        read -r reponse </dev/tty || reponse=""
        case "$reponse" in [oOyY]*) ;; *) info "On garde la valeur existante." ; return 0 ;; esac
    fi
    for essai in 1 2 3; do
        printf '  %s : ' "$question"
        read -r reponse </dev/tty || reponse=""
        # Le jeton s'affiche sur plusieurs lignes dans le terminal : un copier-coller
        # ramene souvent des espaces ou une coupure. Aucun jeton n'en contient.
        reponse="$(printf '%s' "$reponse" | tr -d '[:space:]')"
        if [ -z "$reponse" ]; then
            alerte "Rien n'a été collé — sans ça le bot ne pourra pas démarrer."
            continue
        fi
        if [ -n "$motif" ] && ! printf '%s' "$reponse" | grep -Eq "$motif"; then
            alerte "Ce jeton n'a pas la forme attendue (il est probablement tronqué)."
            [ -n "$conseil" ] && info "$conseil"
            continue
        fi
        ecrire_env "$cle" "$reponse"
        ok "$cle enregistré dans .env"
        return 0
    done
    alerte "On passe. Tu pourras compléter $cle dans .env."
}

valeur_env() {
    [ -f .env ] || { printf ''; return 0; }
    sed -n "s/^$1=//p" .env | head -1
}

# Remplace la ligne si la cle existe, l'ajoute sinon. La valeur n'est jamais reaffichee.
ecrire_env() {
    local cle="$1" valeur="$2" tmp
    tmp="$(mktemp)"
    if [ -f .env ] && grep -q "^$cle=" .env; then
        # La valeur passe par l'environnement : awk -v interpreterait les antislashs.
        FRIPE_VALEUR="$valeur" awk -v k="$cle" \
            'BEGIN{FS=OFS="="} $1==k {print k "=" ENVIRON["FRIPE_VALEUR"]; next} {print}' .env >"$tmp"
    else
        [ -f .env ] && cat .env >"$tmp"
        printf '%s=%s\n' "$cle" "$valeur" >>"$tmp"
    fi
    mv "$tmp" .env
    chmod 600 .env
}

printf '\n%s\n' "${BOLD}Installation de fripe 🧵${OFF}"
printf '%s\n' "${DIM}Le bot Telegram qui retrouve sur Vinted les vêtements d'un TikTok.${OFF}"

# ── 1. La machine est-elle capable de faire tourner le bot ? ──────────────────
titre "1. Vérification de la machine"

ARCH="$(uname -m)"
case "$ARCH" in
    armv7l|armv6l|i386|i686)
        echec "Ton système est en 32 bits ($ARCH). Le SDK Claude n'existe qu'en 64 bits.
  Sur Raspberry Pi : réinstalle Raspberry Pi OS en version 64 bits, puis relance ce script." ;;
esac
ok "Architecture 64 bits ($ARCH)"

PYTHON=""
for candidat in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidat" >/dev/null 2>&1 &&
       "$candidat" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        PYTHON="$candidat"; break
    fi
done
[ -n "$PYTHON" ] || echec "Python 3.11 ou plus récent est nécessaire, et je ne le trouve pas.
  macOS    : brew install python@3.12
  Ubuntu   : sudo apt install python3.12 python3.12-venv
  Windows  : https://www.python.org/downloads/ (coche « Add Python to PATH »)"
ok "$($PYTHON --version)"

# ── 2. Installation du projet ─────────────────────────────────────────────────
titre "2. Installation du projet"

[ -d .venv ] || "$PYTHON" -m venv .venv
ok "Environnement Python prêt (dossier .venv)"

info "Installation des dépendances, ça peut prendre une minute…"
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet .
ok "Dépendances installées"

[ -f .env ] || { cp .env.example .env; chmod 600 .env; }
ok "Fichier de configuration .env prêt"

# ── 3. Le bot Telegram ────────────────────────────────────────────────────────
titre "3. Ton bot Telegram"

info "Sur ton téléphone, ouvre Telegram et écris à ${BOLD}@BotFather${OFF} :"
info "  envoie ${BOLD}/newbot${OFF}, choisis un nom, puis un identifiant finissant par « bot »."
info "Il te répond avec un jeton du genre 123456:ABC-DEF…"
printf '\n'
demander TELEGRAM_BOT_TOKEN "Colle le jeton Telegram ici" \
    '^[0-9]{6,}:[A-Za-z0-9_-]{30,}$' \
    "Il ressemble à 123456789:AAG... — vérifie que tu as bien tout copié."

# ── 4. L'accès à Claude ───────────────────────────────────────────────────────
titre "4. Ton abonnement Claude"

info "Le bot utilise le crédit Agent SDK inclus dans ton abonnement Claude."
info "Pas de carte bancaire, pas de crédits API à acheter."
printf '\n'

if command -v claude >/dev/null 2>&1; then
    if [ -n "$(valeur_env CLAUDE_CODE_OAUTH_TOKEN)" ]; then
        info "Un jeton Claude est déjà enregistré."
    else
        printf '  Lancer « claude setup-token » maintenant ? Il ouvrira ton navigateur. [O/n] '
        read -r rep </dev/tty || rep=""
        case "$rep" in
            [nN]*) info "D'accord. Tu peux le lancer plus tard : claude setup-token" ;;
            *) claude setup-token || alerte "La commande a échoué. Tu peux la relancer plus tard." ;;
        esac
    fi
else
    alerte "L'outil « claude » n'est pas installé sur cette machine."
    info "Installe-le avec :  curl -fsSL https://claude.ai/install.sh | bash"
    info "Puis lance :        claude setup-token"
fi
# Apres l'affichage de claude setup-token, la demande de collage se noie dans
# sa sortie : on la detache visuellement.
printf '\n%s\n' "${DIM}────────────────────────────────────────────────────────${OFF}"
info "${BOLD}Copie le jeton affiché juste au-dessus${OFF} (il commence par sk-ant-oat01-)"
info "et colle-le ici : il n'est enregistré nulle part automatiquement."
printf '\n'
demander CLAUDE_CODE_OAUTH_TOKEN "Colle le jeton Claude (sk-ant-oat01-…)" \
    '^sk-ant-oat01-.{40,}$' \
    "Il s'affiche sur deux lignes : sélectionne bien les DEUX, de sk-ant jusqu'au tout dernier caractère."

# ── 5. Réserver le bot à tes proches ──────────────────────────────────────────
titre "5. Qui a le droit d'utiliser le bot"

info "N'importe qui peut tomber sur un bot Telegram et consommer ton crédit."
info "Une fois le bot lancé, écris-lui ${BOLD}/id${OFF} : il te donne ton identifiant."
info "Ajoute-le ensuite à ALLOWED_CHAT_IDS dans .env (séparés par des virgules)."
info "${DIM}Laisser vide = ouvert à tout le monde, déconseillé.${OFF}"

# ── 6. Vérification ───────────────────────────────────────────────────────────
titre "6. Vérification"

# On ne teste que si la cle attendue PAR CE BACKEND est presente : lancer
# llm-ping avec la mauvaise cle produirait une erreur de configuration
# deguisee en jeton refuse.
VERIF_KO=""
BACKEND_IA="$(valeur_env LLM_BACKEND)"
if [ "$BACKEND_IA" = "anthropic_api" ]; then
    CRED_PRETE="$(valeur_env ANTHROPIC_API_KEY)"
else
    CRED_PRETE="$(valeur_env CLAUDE_CODE_OAUTH_TOKEN)"
fi
if [ -n "$CRED_PRETE" ]; then
    # Un jeton invalide met jusqu'a deux minutes a etre rejete : sans ce mot,
    # l'attente ressemble a un blocage.
    info "Test de l'accès à Claude — jusqu'à deux minutes, c'est normal…"
    if ./.venv/bin/python -m fripe.cli llm-ping; then
        ok "L'accès à Claude fonctionne"
    else
        VERIF_KO="oui"
    fi
fi

# ── Bilan ────────────────────────────────────────────────────────────────────
MANQUANTS=""
[ -n "$(valeur_env TELEGRAM_BOT_TOKEN)" ] || MANQUANTS="$MANQUANTS TELEGRAM_BOT_TOKEN"
if [ "$(valeur_env LLM_BACKEND)" != "anthropic_api" ]; then
    [ -n "$(valeur_env CLAUDE_CODE_OAUTH_TOKEN)" ] || MANQUANTS="$MANQUANTS CLAUDE_CODE_OAUTH_TOKEN"
else
    [ -n "$(valeur_env ANTHROPIC_API_KEY)" ] || MANQUANTS="$MANQUANTS ANTHROPIC_API_KEY"
fi

if [ -n "$VERIF_KO" ]; then
    titre "Le jeton Claude est refusé"
    info "Le jeton enregistré existe, mais Claude le rejette. Deux causes possibles :"
    info "  • il a été ${BOLD}tronqué${OFF} au copier-coller (il s'affiche sur deux lignes) ;"
    info "  • il a été ${BOLD}révoqué${OFF} depuis."
    printf '\n'
    info "Pour repartir sur un jeton neuf :"
    info "  ${BOLD}claude setup-token${OFF}   puis relance ${BOLD}./install.sh${OFF}"
    info "Au moment de coller, sélectionne les deux lignes du jeton, entièrement."
    info "Vérifie aussi ${BOLD}LLM_BACKEND${OFF} dans .env : agent_sdk attend"
    info "CLAUDE_CODE_OAUTH_TOKEN, anthropic_api attend ANTHROPIC_API_KEY."
    printf '\n'
    exit 1
fi

if [ -n "$MANQUANTS" ]; then
    titre "Installation incomplète"
    info "Le projet est installé, mais le bot ${BOLD}ne démarrera pas${OFF} sans ceci :"
    printf '\n'
    for cle in $MANQUANTS; do
        case "$cle" in
            TELEGRAM_BOT_TOKEN)
                printf '  %s✗%s %s — le jeton donné par @BotFather sur Telegram\n' "$RED" "$OFF" "$cle" ;;
            CLAUDE_CODE_OAUTH_TOKEN)
                printf '  %s✗%s %s — lance « claude setup-token » et copie le jeton affiché\n' "$RED" "$OFF" "$cle" ;;
            ANTHROPIC_API_KEY)
                printf '  %s✗%s %s — ta clé API sur platform.claude.com\n' "$RED" "$OFF" "$cle" ;;
        esac
    done
    printf '\n'
    info "Deux façons de compléter :"
    info "  • relance ${BOLD}./install.sh${OFF} (il ne retouchera pas ce qui est déjà bon), ou"
    info "  • ouvre le fichier ${BOLD}.env${OFF} et renseigne la ligne concernée."
    printf '\n'
    exit 1
fi

titre "C'est prêt"

printf '  Lancer le bot :        %s./start.sh%s\n' "$BOLD" "$OFF"
printf '  Tester sans Telegram : %s./.venv/bin/python -m fripe.cli run <lien TikTok>%s\n' "$BOLD" "$OFF"
printf '\n'
info "Ensuite, sur TikTok : Partager → Copier le lien, et envoie-le à ton bot."
printf '\n'
