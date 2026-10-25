"""DATAtourisme event types to the category slugs (spec, Category mapping; the table is the
builder's).

Types are the local names of the URIs in Categories_de_POI ("…/core#Market" is "Market").
A row carries several; the first rule below that one of them matches wins, so the more
specific kinds come first. Types that say nothing about the kind of event (GENERIC) are
ignored, and an event with no mapped type is "autre". The table holds the types seen in
the area's rows (October 2026); a new type falls to "autre" until it is added here.
Lotos carry no type of their own in the export, so the name decides those (LOTO).
"""

from __future__ import annotations

import re
import unicodedata

from agenda.categories import DEFAULT, SLUGS

# (slug, types), in order of precedence.
RULES: list[tuple[str, frozenset[str]]] = [
    ("marche", frozenset({"Market"})),
    ("brocante", frozenset({"GarageSale", "BricABrac"})),
    ("cinema", frozenset({"ScreeningEvent"})),
    ("concert", frozenset({"Concert", "MusicEvent"})),
    ("spectacle", frozenset({"TheaterEvent", "ShowEvent"})),
    ("exposition", frozenset({"Exhibition", "ExhibitionEvent", "VisualArtsEvent", "ArtistSigning"})),
    ("atelier-conference", frozenset({"Conference"})),
    ("loto-jeux", frozenset({"Game"})),
    ("fete", frozenset({"Festival", "TraditionalCelebration", "LocalAnimation", "Parade", "FairOrShow"})),
    ("sport-balade", frozenset({"SportsEvent", "SportsCompetition", "Visit"})),
    # Weaker hints, only when nothing above matched.
    ("fete", frozenset({"SocialEvent", "ReligiousEvent"})),
    ("marche", frozenset({"SaleEvent"})),
]
assert all(slug in SLUGS for slug, _ in RULES)
TYPES: dict[str, str] = {}
for _slug, _types in RULES:
    for _t in _types:
        TYPES.setdefault(_t, _slug)

# On every event, or too broad to tell a kind of event.
GENERIC = frozenset({"PointOfInterest", "EntertainmentAndEvent", "Event", "CulturalEvent", "Product"})

LOTO = re.compile(r"\b(loto|lotos|belote|tarot|jeux? de societe|soiree jeux|escape game)\b")


def _plain(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()


def category_for(types: list[str], name: str = "") -> str:
    """The category slug of an event with these DATAtourisme types (local names)."""
    if name and LOTO.search(_plain(name)):
        return "loto-jeux"
    present = set(types)
    for slug, rule in RULES:
        if present & rule:
            return slug
    return DEFAULT
