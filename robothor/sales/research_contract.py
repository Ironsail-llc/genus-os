"""Native citation selections; persisted CRM dossiers retain full source evidence."""

from pydantic import Field

from robothor.sales.models import DossierFields, EvidenceFact


class PassageEvidence(EvidenceFact):
    source_ref: str = Field(min_length=1, max_length=100)
    passage_ref: str = Field(min_length=1, max_length=100)


class ResearchDossier(DossierFields[PassageEvidence]):
    """Native research selects passages before producing the persisted dossier."""


def passages(content: str) -> list[dict[str, str]]:
    """Version 1: bounded continuous slices, preferring paragraph/word boundaries."""
    rows: list[dict[str, str]] = []
    start = 0
    while start < len(content):
        end = min(start + 800, len(content))
        if end < len(content):
            boundary = content.rfind("\n\n", start + 200, end)
            if boundary < 0:
                boundary = content.rfind(" ", start + 200, end)
            if boundary >= 0:
                end = boundary
        text = content[start:end].strip()
        if text:
            rows.append({"ref": "p" + str(len(rows)), "text": text})
        start = end
        while start < len(content) and content[start].isspace():
            start += 1
    return rows
