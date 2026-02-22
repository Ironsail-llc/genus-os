"""
Pure-Python name matching utilities for contact reconciliation.

No LLM, no DB, no I/O — just algorithmic name comparison.
Used by periodic_analysis.py Phase 4 to link memory entities to CRM contacts.
"""

import re
import unicodedata

# Common nickname → canonical mappings
NICKNAMES = {
    "greg": "gregory",
    "mike": "michael",
    "bob": "robert",
    "rob": "robert",
    "bill": "william",
    "will": "william",
    "jim": "james",
    "jimmy": "james",
    "joe": "joseph",
    "sam": "samantha",
    "sammy": "samantha",
    "dan": "daniel",
    "danny": "daniel",
    "dave": "david",
    "chris": "christopher",
    "tom": "thomas",
    "tony": "anthony",
    "phil": "philip",
    "rick": "richard",
    "dick": "richard",
    "ed": "edward",
    "ted": "edward",
    "alex": "alexander",
    "liz": "elizabeth",
    "beth": "elizabeth",
    "jen": "jennifer",
    "jenny": "jennifer",
    "kate": "katherine",
    "katie": "katherine",
    "matt": "matthew",
    "pat": "patrick",
    "steve": "stephen",
    "nick": "nicholas",
    "andy": "andrew",
    "drew": "andrew",
    "ben": "benjamin",
    "jon": "jonathan",
    "josh": "joshua",
    "charlie": "charles",
    "chuck": "charles",
    "ray": "raymond",
}


def normalize_name(name: str) -> str:
    """Lowercase, strip whitespace, normalize unicode, collapse spaces."""
    if not name:
        return ""
    # Normalize unicode (e.g., accented chars)
    normalized = unicodedata.normalize("NFKD", name)
    # Lowercase
    normalized = normalized.lower().strip()
    # Collapse multiple spaces
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def _split_name(name: str) -> list[str]:
    """Split a normalized name into parts, handling apostrophes."""
    return normalize_name(name).split()


def _canonical(part: str) -> str:
    """Get canonical form of a name part (resolve nicknames)."""
    return NICKNAMES.get(part, part)


def name_similarity(name_a: str, name_b: str) -> float:
    """Multi-signal name similarity scoring (0.0-1.0).

    Signals:
      Exact after normalization: 1.0
      First+Last match: 0.95
      Nickname match (e.g., Greg/Gregory): 0.9
      Prefix match (e.g., Greg/Gregory without nickname table): 0.85
      Single name matches part of full name: 0.8
      Reversed order match: 0.9
      No match: 0.0
    """
    norm_a = normalize_name(name_a)
    norm_b = normalize_name(name_b)

    if not norm_a or not norm_b:
        return 0.0

    # Exact match after normalization
    if norm_a == norm_b:
        return 1.0

    parts_a = _split_name(name_a)
    parts_b = _split_name(name_b)

    # Both have first+last: compare parts
    if len(parts_a) >= 2 and len(parts_b) >= 2:
        # Direct first+last match (ignoring middle names)
        if parts_a[0] == parts_b[0] and parts_a[-1] == parts_b[-1]:
            return 0.95

        # Reversed order match (e.g., "D'Agostino Rizzi" vs "Rizzi D'Agostino")
        if parts_a[0] == parts_b[-1] and parts_a[-1] == parts_b[0]:
            return 0.9

        # Nickname + last name match
        if _canonical(parts_a[0]) == _canonical(parts_b[0]) and parts_a[-1] == parts_b[-1]:
            return 0.9

        # Prefix match on first name + exact last name
        first_a, first_b = parts_a[0], parts_b[0]
        if parts_a[-1] == parts_b[-1]:
            shorter = min(first_a, first_b, key=len)
            longer = max(first_a, first_b, key=len)
            if longer.startswith(shorter) and len(shorter) >= 3:
                return 0.85

        return 0.0

    # One name is single, other is multi-part
    if len(parts_a) == 1 or len(parts_b) == 1:
        single = parts_a[0] if len(parts_a) == 1 else parts_b[0]
        multi = parts_b if len(parts_a) == 1 else parts_a

        # Single name matches any part of the multi-part name
        if single in multi:
            return 0.8

        # Single name is canonical form of a part
        for part in multi:
            if _canonical(single) == _canonical(part):
                return 0.8

        # Single name is a prefix of a part (e.g., "Greg" vs "Gregory Smith")
        for part in multi:
            if single == part:
                continue  # Already handled above
            if len(single) < len(part) and part.startswith(single) and len(single) >= 3:
                return 0.75
            if len(part) < len(single) and single.startswith(part) and len(part) >= 3:
                return 0.75

    return 0.0


def find_best_match(
    name: str,
    candidates: list[dict],
    threshold: float = 0.75,
    name_key: str = "name",
) -> dict | None:
    """Find the best matching candidate above threshold.

    Args:
        name: The name to match.
        candidates: List of dicts, each must have a key specified by name_key.
        threshold: Minimum similarity score (0.0-1.0).
        name_key: Key in candidate dicts that holds the name string.

    Returns:
        Best matching candidate dict (with 'match_score' added), or None.
        On ties, prefers candidate with higher 'mention_count' (if present).
    """
    if not name or not candidates:
        return None

    best_match = None
    best_score = 0.0

    for candidate in candidates:
        candidate_name = candidate.get(name_key, "")
        if not candidate_name:
            continue

        score = name_similarity(name, candidate_name)
        if score < threshold:
            continue

        if score > best_score:
            best_score = score
            best_match = candidate
        elif score == best_score and best_match is not None:
            # Prefer higher mention count on ties
            if candidate.get("mention_count", 0) > best_match.get("mention_count", 0):
                best_match = candidate

    if best_match is not None:
        result = dict(best_match)
        result["match_score"] = best_score
        return result

    return None
