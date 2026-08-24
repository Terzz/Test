"""Prompts francais envoyes au modele pour l'analyse des slides et le re-classement."""

from __future__ import annotations

from fripe.catalog import BRANDS, CATALOGS, COLORS, format_table

# Les tables sont injectees telles quelles : le modele ne doit jamais inventer un
# identifiant, un id absent des tables est de toute facon filtre par models.Garment.
_TABLES = (
    f"Categories (catalog_id) : {format_table(CATALOGS)}\n"
    f"Couleurs (color_ids) : {format_table(COLORS)}\n"
    f"Marques (brand_id) : {format_table(BRANDS)}"
)

_ANALYSIS_RULES = f"""Tu identifies les vêtements portés sur les photos d'un diaporama TikTok, puis tu écris les requêtes qui permettront de retrouver des pièces équivalentes parmi les annonces d'occasion de vinted.fr.

RÈGLE 1 — UNE ENTRÉE PAR PIÈCE, SANS DOUBLON
Recense chaque vêtement ou accessoire DISTINCT vu sur l'ensemble des slides. La même veste vue sur trois slides est UNE SEULE entrée : choisis la slide où elle est la plus nette et la plus complète, et donne ce numéro dans slide_index. Ne crée jamais deux entrées pour la même pièce, même si elle change d'angle, de lumière ou de pose. Ignore l'arrière-plan, le décor et tout ce qui ne s'achète pas, ainsi que les pièces trop floues ou trop cachées pour être décrites honnêtement. En général une tenue donne 2 à 6 entrées.

RÈGLE 2 — ÉCRIS COMME UN VENDEUR QUI N'Y CONNAÎT RIEN EN MODE
Les annonces Vinted sont rédigées par des particuliers ordinaires qui décrivent leurs affaires platement, avec les mots de tout le monde. C'est ce texte-là que ta requête doit rencontrer, pas celui d'un magazine.
Une requête = catégorie + couleur + matière + coupe, et la marque seulement si un logo est lisible sur la photo.
Bons exemples : « veste en cuir marron », « pull col roulé beige », « jean large bleu clair », « bottes cuir noir à talon », « sac bandoulière marron ».

RÈGLE 3 — VOCABULAIRE INTERDIT
N'utilise jamais de vocabulaire de tendance ou d'esthétique : « y2k », « gorpcore », « old money », « coquette », « streetwear », « aesthetic », « core », « quiet luxury », « clean girl », « grunge », ni aucun autre nom de micro-tendance, ni le jargon mode anglais. Aucun particulier n'écrit ces mots dans son annonce : une requête qui en contient ne remonte rien.

RÈGLE 4 — 2 OU 3 VARIANTES, DE LA PLUS PRÉCISE À LA PLUS GÉNÉRIQUE
La première variante décrit la pièce en détail, la dernière reste très simple (2 ou 3 mots) pour ratisser large. Change de synonyme d'une variante à l'autre, car les vendeurs n'emploient pas tous le même mot : pull / sweat, veste / blouson / perfecto, jean / pantalon en jean, baskets / tennis, sac / sacoche, manteau / parka.

RÈGLE 5 — IDENTIFIANTS DU CATALOGUE
Choisis catalog_id, color_ids et brand_id UNIQUEMENT dans les tables ci-dessous. Un identifiant inventé filtre la recherche sur la mauvaise catégorie et ne renvoie rien : au moindre doute, mets null ou [] plutôt que de deviner. Si tu hésites entre une catégorie femme et une catégorie homme, mets null. Donne une seule couleur, deux au maximum si la pièce est vraiment bicolore.
{_TABLES}

RÈGLE 6 — CADRE DE LA PIÈCE (bbox)
Donne la zone occupée par la pièce dans sa slide, en coordonnées normalisées entre 0 et 1 : x et y = coin haut-gauche, w et h = largeur et hauteur. Approximatif suffit, ce cadre sert seulement à découper une vignette. Mets null si tu ne sais pas la situer.

"""

_ANALYSIS_FORMAT = """FORMAT DE RÉPONSE
Réponds avec UN SEUL bloc JSON, sans aucun texte avant ni après, exactement à ce schéma :

```json
{
  "garments": [
    {
      "id": "veste-cuir",
      "label_fr": "veste en cuir marron",
      "queries_fr": ["veste en cuir marron vintage", "blouson cuir marron", "veste marron"],
      "catalog_id": 1908,
      "color_ids": [2],
      "brand": null,
      "brand_id": null,
      "slide_index": 2,
      "bbox": {"x": 0.18, "y": 0.12, "w": 0.6, "h": 0.45},
      "confidence": "high"
    }
  ]
}
```

Champs :
- id : identifiant court, en minuscules et sans espace, unique dans la réponse (« veste-cuir », « jean-large »).
- label_fr : nom court de la pièce en français, affiché tel quel à l'utilisateur.
- queries_fr : 2 ou 3 requêtes, de la plus précise à la plus générique.
- catalog_id : un entier de la table des catégories, sinon null.
- color_ids : liste d'entiers de la table des couleurs, sinon [].
- brand : nom de la marque si un logo est lisible, sinon null.
- brand_id : entier de la table des marques si elle y figure, sinon null.
- slide_index : numéro de la slide où la pièce est la plus nette (1 = première image).
- bbox : cadre de la pièce dans cette slide, sinon null.
- confidence : « high », « medium » ou « low »."""

ANALYSIS_SYSTEM = _ANALYSIS_RULES + _ANALYSIS_FORMAT

ANALYSIS_USER = """Identifie chaque pièce distincte de la tenue.

Rappel : une pièce = une seule entrée, même si elle apparaît sur plusieurs slides. Écris les requêtes comme un particulier qui revend ses affaires (catégorie + couleur + matière + coupe), sans aucun mot de tendance. Ne prends les identifiants que dans les tables fournies et mets null ou [] au moindre doute.

Réponds avec un seul bloc JSON conforme au schéma."""

RERANK_SYSTEM = """Tu compares des photos de vêtements pour un moteur de recherche de seconde main.

L'image 1 est la pièce recherchée, découpée dans une photo de tenue. Les images suivantes (image 2, image 3, etc.) sont les photos des annonces Vinted à classer, une image par annonce.

Classe les annonces de la plus ressemblante à la moins ressemblante à l'image 1, en comparant dans cet ordre d'importance :
1. le type de pièce et la coupe (longueur, volume, col, manches) ;
2. la matière et son aspect (cuir, jean, laine, maille, synthétique brillant…) ;
3. la couleur et sa nuance exacte ;
4. le motif (uni, rayé, à carreaux, imprimé) ;
5. les détails : boutons, fermeture, poches, ceinture, coutures, doublure.

Ignore complètement l'arrière-plan, l'éclairage, la qualité de l'image, la pose, le mannequin, le cintre et la mise en scène : ce sont des photos prises à la maison par des particuliers, elles ne ressembleront jamais à une photo de mode. Une annonce qui montre une pièce d'un autre type qu'à l'image 1 se classe en dernier, mais reste dans le classement.

FORMAT DE RÉPONSE
Réponds avec UN SEUL bloc JSON, sans aucun texte avant ni après :

```json
{"ranking": [4, 2, 7, 3, 5, 6], "exact_match_candidates": [4], "note_fr": ""}
```

- ranking : les NUMÉROS D'IMAGE des annonces (donc à partir de 2, jamais 1), de la plus ressemblante à la moins ressemblante. Chaque annonce apparaît une fois et une seule.
- exact_match_candidates : les numéros d'image des annonces qui semblent être exactement le même produit (même modèle, pas seulement le même style). Liste vide si aucune.
- note_fr : une remarque courte en français pour l'utilisateur, ou "" s'il n'y a rien à signaler."""

RERANK_USER = """Classe les annonces de la plus ressemblante à la moins ressemblante à l'image 1, puis réponds avec un seul bloc JSON conforme au schéma."""


def analysis_user_text(n_slides: int) -> str:
    """Instruction accompagnant les slides, avec le nombre d'images envoyees."""
    count = max(int(n_slides), 0)
    if count == 0:
        return ANALYSIS_USER
    if count == 1:
        intro = "Voici l'unique photo du diaporama TikTok (image 1 = slide 1)."
    else:
        intro = (
            f"Voici les {count} photos du diaporama TikTok, dans l'ordre : "
            "image 1 = slide 1, image 2 = slide 2, et ainsi de suite."
        )
    return f"{intro}\n\n{ANALYSIS_USER}"


def rerank_user_text(garment_label: str, n_candidates: int) -> str:
    """Instruction accompagnant le crop source (image 1) et les vignettes."""
    label = " ".join(str(garment_label).split()) or "la pièce de l'image 1"
    count = max(int(n_candidates), 0)
    if count == 0:
        return f"Pièce recherchée : « {label} » (image 1).\n\n{RERANK_USER}"
    scope = "l'image 2" if count == 1 else f"les images 2 à {count + 1}"
    plural = "" if count == 1 else "s"
    intro = (
        f"Pièce recherchée : « {label} » (image 1).\n"
        f"{count} annonce{plural} à classer : {scope}."
    )
    return f"{intro}\n\n{RERANK_USER}"


__all__ = [
    "ANALYSIS_SYSTEM",
    "ANALYSIS_USER",
    "RERANK_SYSTEM",
    "RERANK_USER",
    "analysis_user_text",
    "rerank_user_text",
]
