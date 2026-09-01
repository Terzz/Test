#!/usr/bin/env bash
# Diagnostic complet : teste chaque etage de la chaine sur CETTE machine et
# imprime un rapport a copier-coller. N'affiche jamais le contenu des jetons.
#
#   ./diagnostic.sh [lien TikTok]

cd "$(dirname "$0")" || exit 1

LIEN="${1:-https://vm.tiktok.com/ZGdxGpLHD/}"
PY=./.venv/bin/python

titre() { printf '\n────────────────────────────────────────────────────────\n%s\n────────────────────────────────────────────────────────\n' "$1"; }
ok()    { printf '  [OK]    %s\n' "$1"; }
ko()    { printf '  [ECHEC] %s\n' "$1"; }
info()  { printf '          %s\n' "$1"; }

RESUME=""
note() { RESUME="$RESUME
$1"; }

printf 'DIAGNOSTIC FRIPE — %s\n' "$(date '+%Y-%m-%d %H:%M')"
printf 'lien teste : %s\n' "$LIEN"

# ── 1. Machine ────────────────────────────────────────────────────────────────
titre "1. Machine et installation"

printf '  systeme      : %s %s\n' "$(uname -s)" "$(uname -m)"
if [ -x "$PY" ]; then
    ok "environnement Python : $($PY --version 2>&1)"
else
    ko "pas d'environnement Python (.venv absent) — lance ./install.sh"
    note "installation absente"
    printf '%s\n' "$RESUME"; exit 1
fi

$PY -c "import fripe" 2>/dev/null \
    && ok "paquet fripe importable" \
    || { ko "paquet fripe non installe — lance : ./.venv/bin/pip install -e ."; note "paquet non installe"; }

$PY -c "import gallery_dl" 2>/dev/null \
    && ok "gallery-dl present (extracteur de secours)" \
    || info "gallery-dl absent — le repli TikTok ne sera pas disponible"

$PY -c "import curl_cffi" 2>/dev/null \
    && ok "curl_cffi present (contournement anti-bot)" \
    || ko "curl_cffi absent : TikTok et Vinted repondront 403"

if [ "$(uname -s)" = Darwin ]; then
    if launchctl print "gui/$(id -u)/com.fripe.bot" >/dev/null 2>&1; then
        PID_BOT="$(launchctl print "gui/$(id -u)/com.fripe.bot" 2>/dev/null | sed -n 's/^[[:space:]]*pid = \([0-9][0-9]*\).*/\1/p' | head -1)"
        if [ -n "$PID_BOT" ]; then
            ok "demarrage automatique actif, bot en cours (pid $PID_BOT)"
        else
            info "demarrage automatique installe mais bot arrete — voir ./autostart.sh status"
            note "bot automatique arrete"
        fi
    else
        info "demarrage automatique non installe (./autostart.sh)"
    fi
fi

# ── 2. Configuration ──────────────────────────────────────────────────────────
titre "2. Configuration (.env)"

valeur() { [ -f .env ] && sed -n "s/^$1=//p" .env | head -1; }

# La cle IA attendue depend du backend configure.
BACKEND_IA="$(valeur LLM_BACKEND)"
if [ "$BACKEND_IA" = "anthropic_api" ]; then CLE_IA=ANTHROPIC_API_KEY; else CLE_IA=CLAUDE_CODE_OAUTH_TOKEN; fi
info "backend IA : ${BACKEND_IA:-agent_sdk}"

for cle in TELEGRAM_BOT_TOKEN "$CLE_IA"; do
    if [ -n "$(valeur "$cle")" ]; then
        ok "$cle renseigne ($(valeur "$cle" | wc -c | tr -d ' ') caracteres)"
    else
        ko "$cle vide"
        note "$cle manquant dans .env"
    fi
done

if [ -n "$(valeur ALLOWED_CHAT_IDS)" ]; then
    ok "ALLOWED_CHAT_IDS renseigne (bot prive)"
else
    info "ALLOWED_CHAT_IDS vide : n'importe qui peut utiliser ton bot"
    note "bot ouvert a tout le monde (ALLOWED_CHAT_IDS vide)"
fi

if [ "$BACKEND_IA" != "anthropic_api" ] && [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    ko "ANTHROPIC_API_KEY presente dans l'environnement : elle facturerait des credits API"
    note "ANTHROPIC_API_KEY traine dans l'environnement"
else
    ok "pas de cle API parasite pour ce backend"
fi

# ── 3. Telegram ───────────────────────────────────────────────────────────────
titre "3. Telegram"

JETON="$(valeur TELEGRAM_BOT_TOKEN)"
if [ -n "$JETON" ]; then
    REPONSE="$(curl -sS --max-time 20 "https://api.telegram.org/bot$JETON/getMe" 2>&1)"
    case "$REPONSE" in
        *'"ok":true'*)
            NOM="$(printf '%s' "$REPONSE" | sed -n 's/.*"username":"\([^"]*\)".*/\1/p')"
            ok "jeton valide, bot @$NOM" ;;
        *'"ok":false'*)
            ko "Telegram refuse le jeton : $(printf '%s' "$REPONSE" | sed -n 's/.*"description":"\([^"]*\)".*/\1/p')"
            note "jeton Telegram refuse" ;;
        *)  ko "pas de reponse de Telegram (reseau ?)"
            note "Telegram injoignable" ;;
    esac
else
    ko "jeton absent, test saute"
fi

# ── 4. Extraction TikTok ──────────────────────────────────────────────────────
titre "4. Extraction TikTok (c'est ici que ca coincait)"

$PY - "$LIEN" <<'PYEOF'
import asyncio, logging, sys
logging.basicConfig(level=logging.WARNING, format="          %(levelname)s %(name)s | %(message)s")
from fripe import tiktok

async def main():
    url = sys.argv[1]
    canonique = await tiktok._resolve_short_url(url)
    print(f"          lien deplie : {canonique or 'echec du depliage'}")
    for nom, fn in (("curl_cffi", tiktok._tikwm_via_curl_cffi), ("httpx", tiktok._tikwm_via_httpx)):
        try:
            d = await fn(url)
            print(f"  [OK]    tikwm via {nom} : code={d.get('code')}")
        except Exception as e:
            print(f"  [ECHEC] tikwm via {nom} : {type(e).__name__}: {str(e)[:90]}")
    try:
        post = await tiktok.fetch_slides(url)
        print(f"  [OK]    extraction : {len(post.image_urls)} images, post {post.post_id}")
    except tiktok.VideoPost:
        print("  [INFO]  ce lien est une VIDEO, pas un diaporama photo")
    except Exception as e:
        print(f"  [ECHEC] extraction : {type(e).__name__}: {str(e)[:120]}")

asyncio.run(main())
PYEOF

# ── 5. Vinted ─────────────────────────────────────────────────────────────────
titre "5. Recherche Vinted"

$PY - <<'PYEOF'
import asyncio, logging
from pathlib import Path
logging.basicConfig(level=logging.WARNING, format="          %(levelname)s %(name)s | %(message)s")
from fripe.vinted import VintedClient

async def main():
    client = VintedClient(Path("data/vinted_cookies.json"))
    try:
        items = await client.search("veste cuir marron", catalog_ids=[1908], color_ids=[2], per_page=5)
        print(f"  [OK]    {len(items)} annonces recuperees")
        for it in items[:3]:
            print(f"          {it.price_label():>8} | {it.title[:44]}")
    except Exception as e:
        print(f"  [ECHEC] {type(e).__name__}: {str(e)[:140]}")
    finally:
        await client.close()

asyncio.run(main())
PYEOF

# ── 6. Acces au modele ────────────────────────────────────────────────────────
titre "6. Acces a Claude (jusqu'a 2 minutes)"

# Le code de sortie doit etre lu AVANT tout tuyau, sinon c'est celui de sed.
SORTIE_PING="$($PY -m fripe.cli llm-ping 2>&1)"
CODE_PING=$?
printf '%s\n' "$SORTIE_PING" | sed 's/^/          /'
if [ "$CODE_PING" -eq 0 ]; then
    ok "le modele repond"
    CLAUDE_OK=1
else
    ko "le modele ne repond pas"
    note "acces Claude en echec"
    CLAUDE_OK=0
fi

# ── 7. Chaine complete ────────────────────────────────────────────────────────
titre "7. Chaine complete (analyse + recherche + classement)"

if [ "${CLAUDE_OK:-0}" -eq 1 ]; then
    info "Cette etape consomme un peu de credit et prend 1 a 2 minutes…"
    SORTIE_RUN="$($PY -m fripe.cli run "$LIEN" 2>&1)"
    CODE_RUN=$?
    printf '%s\n' "$SORTIE_RUN" | tail -45 | sed 's/^/          /'
    if [ "$CODE_RUN" -eq 0 ]; then
        ok "chaine complete executee"
    else
        ko "la chaine complete a echoue"
        note "chaine complete en echec"
    fi
else
    info "Etape sautee : sans acces a Claude, l'analyse ne peut pas tourner."
fi

# ── Resume ────────────────────────────────────────────────────────────────────
titre "RESUME"

if [ -z "$RESUME" ]; then
    printf '  Aucun probleme bloquant detecte.\n'
else
    printf '  Points a regarder :%s\n' "$RESUME"
fi
printf '\n  Copie tout ce rapport et colle-le dans la conversation.\n'
printf '  Aucun jeton n%s figure : seule leur longueur est indiquee.\n\n' "'y"
