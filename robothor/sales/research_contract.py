"""Native citation selections; persisted CRM dossiers retain full source evidence."""

import re

from pydantic import Field

from robothor.sales.models import DossierFields, EvidenceFact


class PassageEvidence(EvidenceFact):
    source_ref: str = Field(min_length=1, max_length=100)
    passage_ref: str = Field(min_length=1, max_length=100)


class ResearchDossier(DossierFields[PassageEvidence]):
    """Native research selects passages before producing the persisted dossier."""


def passages(content: str, *, version: int = 1) -> list[dict[str, str]]:
    """V2 keeps paragraphs separate; V1 remains stable for saved source proofs."""
    if version == 2:
        parts = [
            row["text"]
            for paragraph in re.split(r"\n[ \t]*\n", content)
            for row in passages(paragraph, version=1)
        ]
        return [{"ref": "p" + str(i), "text": text} for i, text in enumerate(parts)]
    if version != 1:
        raise ValueError("Unsupported captured passage version")
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
