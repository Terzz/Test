"""Tables d'identifiants Vinted (source unique, utilisee par models, prompts et vinted).

Valeurs relevees sur les endpoints publics /api/v2/colors, /api/v2/brands et sur
l'arbre `catalogTree` embarque dans les pages catalogue de vinted.fr.
"""

from __future__ import annotations

# Categories. Les identifiants larges (1037, 4) ratissent plus que les feuilles
# (1908) : le prompt d'analyse prefere le plus precis qui reste sur.
CATALOGS: dict[int, str] = {
    1904: "Femmes",
    4: "Femmes > Vetements",
    1037: "Femmes > Manteaux et vestes",
    1907: "Femmes > Manteaux",
    1908: "Femmes > Vestes",
    1079: "Femmes > Vestes en jean",
    16: "Femmes > Chaussures",
    19: "Femmes > Sacs",
    5: "Hommes",
    2050: "Hommes > Vetements",
    1206: "Hommes > Manteaux et vestes",
    2052: "Hommes > Vestes",
}

COLORS: dict[int, str] = {
    1: "Noir",
    2: "Marron",
    3: "Gris",
    4: "Beige",
    5: "Fuchsia",
    6: "Violet",
    7: "Rouge",
    8: "Jaune",
    9: "Bleu",
    10: "Vert",
    12: "Blanc",
    13: "Argente",
    14: "Dore",
    15: "Multicolore",
    16: "Kaki",
    20: "Creme",
    23: "Bordeaux",
    24: "Rose",
    26: "Bleu clair",
    27: "Marine",
}

# Volontairement court : le modele ne renseigne une marque que si un logo est
# lisible sur la photo. Toute marque hors de cette table est ignoree cote filtre
# mais reste utilisable dans le texte de recherche.
BRANDS: dict[int, str] = {
    14: "adidas",
    53: "Nike",
    255: "Calvin Klein",
}

BRAND_IDS_BY_NAME: dict[str, int] = {name.lower(): id_ for id_, name in BRANDS.items()}


def format_table(table: dict[int, str]) -> str:
    """Rend une table lisible pour l'injection dans un prompt."""
    return ", ".join(f"{id_}={name}" for id_, name in table.items())
