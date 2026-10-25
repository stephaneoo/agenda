"""The fixed category list (spec, Category mapping). Adding a slug is a non-breaking change."""

SLUGS: dict[str, str] = {
    "concert": "Concert",
    "spectacle": "Spectacle",
    "cinema": "Cinéma",
    "exposition": "Exposition",
    "marche": "Marché",
    "brocante": "Brocante, vide-greniers",
    "fete": "Fête",
    "sport-balade": "Sport, balade",
    "atelier-conference": "Atelier, conférence",
    "loto-jeux": "Loto, jeux",
    "autre": "Autre",
}

DEFAULT = "autre"
