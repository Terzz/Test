# fripe 🧵

Bot Telegram : tu lui envoies le lien d'un **TikTok en mode photo** (un diaporama de tenues), il retrouve chaque pièce sur **Vinted** et te renvoie les annonces.

## L'idée

Une vendeuse de 50 ans ne mettra jamais « y2k » dans son annonce : elle écrit « veste en cuir marron ». Le bot analyse donc les photos et génère des requêtes en **langage vendeur** — catégorie, couleur, matière, coupe — plutôt qu'en vocabulaire de tendance. Les résultats sont ensuite reclassés visuellement pour remonter ceux qui ressemblent vraiment à la pièce d'origine.

## Comment ça marche

```
Lien TikTok → images des slides → analyse visuelle (Claude)
  → requêtes « langage vendeur » + catégorie + couleur
  → recherche Vinted (filtres relâchés progressivement si peu de résultats)
  → reclassement visuel des annonces
  → un album Telegram par vêtement
```

## Installation

### Prérequis

- **Python 3.11+**
- Sur Raspberry Pi : **un OS 64 bits obligatoire** (`uname -m` doit afficher `aarch64`). Il n'existe pas de version du SDK Claude pour les OS 32 bits.
- Un abonnement **Claude Pro ou Max** (le bot utilise le crédit Agent SDK inclus dans l'abonnement), ou à défaut une clé API Anthropic.

### Installation en une commande

```bash
git clone <ce dépôt> fripe && cd fripe
./install.sh
```

Le script vérifie ta machine, installe tout, puis te guide pas à pas pour les deux
jetons (Telegram et Claude). Tu peux le relancer sans risque : il ne remplace
jamais une valeur existante sans te demander.

Ensuite, pour lancer le bot :

```bash
./start.sh
```

Sur Mac, pour ne plus avoir à y penser (démarrage au login, rattrapage des liens
reçus pendant que l'ordi était éteint) : `./autostart.sh` — voir plus bas.

Le reste de cette section détaille ce que fait le script, si tu préfères le faire
à la main ou comprendre chaque étape.

### 1. Le projet

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install .
cp .env.example .env
```

### 2. Créer le bot Telegram

Sur Telegram, écris à **@BotFather**, envoie `/newbot`, choisis un nom, et copie le jeton dans `.env` :

```
TELEGRAM_BOT_TOKEN=123456:ABC-...
```

### 3. Connecter ton abonnement Claude

Le bot utilise le **Claude Agent SDK** officiel, couvert par le crédit mensuel inclus dans les abonnements Claude depuis juin 2026 — pas besoin d'acheter des crédits API.

```bash
claude setup-token     # ouvre le navigateur, connecte-toi avec ton compte Claude
```

Colle le jeton affiché dans `.env` :

```
LLM_BACKEND=agent_sdk
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
```

> ⚠️ Si une variable `ANTHROPIC_API_KEY` traîne dans ton environnement, elle prendrait le pas sur l'abonnement et te facturerait des crédits API. Le bot la retire automatiquement au démarrage et te prévient dans les logs.

**Alternative avec une clé API** (facturation à l'usage) : mets `LLM_BACKEND=anthropic_api` et renseigne `ANTHROPIC_API_KEY`.

### 4. Réserver le bot à tes proches

N'importe qui peut trouver un bot Telegram et consommer ton crédit : le bot ne répond donc qu'aux chats listés. Écris `/id` à ton bot pour obtenir l'identifiant de ton chat, puis liste ceux que tu autorises :

```
ALLOWED_CHAT_IDS=123456789,987654321
```

Tant que la liste est vide, le bot refuse tout le monde (et te donne ton identifiant). `ALLOWED_CHAT_IDS=*` l'ouvre à tout le monde, en connaissance de cause. Tout changement de `.env` demande de relancer le bot (`./autostart.sh restart` ou Ctrl+C puis `./start.sh`).

### 5. Lancer

```bash
./start.sh
```

Envoie-lui un lien TikTok (**Partager → Copier le lien**) : compte 2 à 4 minutes, le message de statut te tient au courant. Ne lance pas `./start.sh` en plus du démarrage automatique : deux bots sur le même jeton se volent les messages.

## Tester étape par étape

Chaque morceau du pipeline est utilisable seul, sans Telegram :

```bash
python -m fripe.cli llm-ping                                  # l'accès au modèle fonctionne ?
python -m fripe.cli slides  https://vm.tiktok.com/XXXX/       # télécharge les images
python -m fripe.cli vinted  "veste cuir marron" --catalog 1908 --color 2
python -m fripe.cli analyze https://vm.tiktok.com/XXXX/       # vêtements détectés (JSON)
python -m fripe.cli run     https://vm.tiktok.com/XXXX/       # chaîne complète
```

## Faire tourner en continu

**Mac (démarrage automatique + rattrapage)** :

```bash
./autostart.sh
```

Le bot démarre à chaque ouverture de session et redémarre s'il plante. Les liens
envoyés pendant que le Mac était éteint sont traités au réveil : Telegram les
garde 24 heures. Le Mac verrouillé convient ; en veille, le bot est en pause et
reprend au réveil (une recherche interrompue par la veille est relancée une fois).
Après un redémarrage du Mac, le bot repart dès que tu ouvres ta session.

Pour un bot vraiment disponible 24h/24 sans rien acheter : dans Réglages Système →
Batterie → Adaptateur secteur, active « Empêcher la mise en veille automatique
lorsque l'écran est éteint ». Branché, le Mac reste alors éveillé (écran éteint) et
le bot répond en continu.

macOS affiche à l'installation « python a ajouté des éléments pouvant s'exécuter en
arrière-plan » : c'est ce bot, laisse-le autorisé (Réglages Système → Général →
Ouverture).

```bash
./autostart.sh status     # l'état et les dernières lignes du journal
./autostart.sh logs       # le journal en direct
./autostart.sh restart    # après un git pull ou un changement de .env
./autostart.sh off        # retire le démarrage automatique
```

Le journal est dans `data/logs/bot.log` (tournant, 3 × 2 Mo maximum, jetons masqués).
Pour plus de détail : `LOG_LEVEL=DEBUG` dans `.env` puis `./autostart.sh restart`.

**systemd** (Raspberry Pi, machine perso) : voir `deploy/fripe.service`, à adapter aux chemins de ta machine.

**Docker** :

```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

Le bot fonctionne en *long polling* : aucun port à ouvrir, aucune IP publique, il marche derrière une box.

## Réglages utiles (`.env`)

| Variable | Effet |
|---|---|
| `ANALYSIS_MODEL` | Modèle d'analyse des photos (défaut `claude-opus-5`) |
| `RERANK_MODEL` | Modèle de reclassement des vignettes (défaut `claude-haiku-4-5`) |
| `MAX_RESULTS_PER_GARMENT` | Annonces par vêtement, entre 2 et 10 (défaut 6) |
| `PRICE_TO` | Prix maximum en euros |
| `ALLOWED_CHAT_IDS` | Liste blanche des chats autorisés |

## Limites connues

- **Diaporamas photo uniquement.** Un lien vidéo est refusé avec un message clair.
- L'extraction TikTok passe par un service tiers (tikwm), avec `gallery-dl` en secours : si les deux tombent, le bot le dit.
- La recherche Vinted utilise leur API interne non officielle, sans garantie de stabilité. Le bot reste volontairement discret (une poignée de requêtes espacées) — n'en fais pas un outil de masse. Quand Vinted refuse, le bot fait une pause de 10 minutes sans lancer d'analyse.
- Le lien TikTok (sans ses paramètres de suivi) et ton adresse IP sont transmis à tikwm.com, le service tiers qui récupère les images.
- Le crédit Agent SDK inclus dans l'abonnement est plafonné mensuellement ; au-delà, l'usage bascule en facturation à l'usage.
