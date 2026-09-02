"""Lecture des reponses du modele : le JSON arrive rarement propre."""

from __future__ import annotations

import pytest

from fripe.llm import LLMError, build_content, extract_json
from fripe.models import ImagePart


def test_bloc_json_balise():
    texte = 'Voici le résultat :\n```json\n{"garments": [{"id": "g1"}]}\n```\nBonne recherche !'
    assert extract_json(texte)["garments"][0]["id"] == "g1"


def test_bloc_sans_langage():
    assert extract_json('```\n{"ok": true}\n```') == {"ok": True}


def test_json_nu():
    assert extract_json('{"ok": true}') == {"ok": True}


def test_json_entoure_de_prose():
    texte = 'Après analyse des photos : {"garments": []} — voilà.'
    assert extract_json(texte) == {"garments": []}


def test_accolade_dans_une_chaine_ne_trompe_pas_le_parseur():
    texte = '{"label_fr": "veste { bizarre", "note": "guillemet \\" échappé"}'
    resultat = extract_json(texte)
    assert resultat["label_fr"] == "veste { bizarre"
    assert resultat["note"] == 'guillemet " échappé'


def test_liste_de_vetements_conservee_entiere():
    # Une liste nue ne doit pas etre tronquee a son premier element.
    resultat = extract_json('[{"id": "g1"}, {"id": "g2"}, {"id": "g3"}]')
    valeurs = next(v for v in resultat.values() if isinstance(v, list))
    assert [entry["id"] for entry in valeurs] == ["g1", "g2", "g3"]


@pytest.mark.parametrize(
    "texte",
    ["", "je n'ai rien trouvé", "```json\n{tronqué", "[1, 2, 3]"],
)
def test_reponses_illisibles(texte):
    with pytest.raises(LLMError) as exc:
        extract_json(texte)
    assert exc.value.user_message_fr


def test_build_content_alterne_consignes_et_images():
    parts = [
        ImagePart(data_b64="AAA", label="Image 1:"),
        ImagePart(data_b64="BBB", label="Image 2:"),
    ]
    blocs = build_content(parts, "Analyse ces photos.")

    assert blocs[0] == {"type": "text", "text": "Analyse ces photos."}
    assert [b["type"] for b in blocs] == ["text", "text", "image", "text", "image"]
    assert blocs[2]["source"]["data"] == "AAA"
    assert blocs[2]["source"]["type"] == "base64"
    assert blocs[3]["text"] == "Image 2:"


def test_build_content_sans_image():
    blocs = build_content([], "Ping")
    assert blocs == [{"type": "text", "text": "Ping"}]


def test_classify_failure_distingue_jeton_et_quota():
    from fripe.llm import _AUTH_FR, _GENERIC_FR, _QUOTA_FR, classify_failure, looks_like_failure

    assert classify_failure("API Error: 401 authentication_error: invalid x-api-key").user_message_fr == _AUTH_FR
    assert classify_failure("OAuth token has expired. Please run /login").user_message_fr == _AUTH_FR
    assert classify_failure("You've hit your usage limit · resets 11:50pm").user_message_fr == _QUOTA_FR
    assert classify_failure("API Error: 429 rate_limit_error").user_message_fr == _QUOTA_FR
    assert classify_failure("quelque chose d'autre").user_message_fr == _GENERIC_FR
    assert looks_like_failure("usage limit reached") and not looks_like_failure('{"garments": []}')
