#!/usr/bin/env python3
"""
Google Meet Transcript Sync — System Cron Script

Fetches "Notes by Gemini" documents from Robothor's Google Drive,
parses them into structured sections (summary, decisions, next steps,
transcript), and writes to memory/meet-transcripts.json.

Pattern: Same as email_sync.py — fetch external data, write JSON log.
Ingestion is handled by continuous_ingest.py (ingest_google_meet).

Cron: */10 * * * *
"""

import fcntl
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

LOG_PATH = Path("/home/philip/robothor/brain/memory/meet-transcripts.json")
STATE_PATH = Path("/home/philip/robothor/brain/memory/meet-transcript-state.json")
CALENDAR_PATH = Path("/home/philip/robothor/brain/memory/calendar-log.json")
LOCK_PATH = Path("/home/philip/robothor/brain/memory/.meet-transcript.lock")
GOG_PASSWORD = os.environ["GOG_KEYRING_PASSWORD"]
ACCOUNT = "robothor@ironsail.ai"


def run_gog(args: list[str]) -> str:
    """Run gog command and return stdout."""
    env = os.environ.copy()
    env["GOG_KEYRING_PASSWORD"] = GOG_PASSWORD
    result = subprocess.run(
        ["gog"] + args,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout


def load_json(path: Path) -> dict:
    """Load JSON file or return empty dict on failure."""
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def save_json(data: dict, path: Path):
    """Atomic save — temp file then rename."""
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def fetch_gemini_docs() -> list[dict]:
    """Search Drive for 'Notes by Gemini' documents."""
    output = run_gog(
        [
            "drive",
            "search",
            "Notes by Gemini",
            "--account",
            ACCOUNT,
            "--json",
        ]
    )
    if not output.strip():
        return []
    try:
        data = json.loads(output)
        return data.get("files", [])
    except json.JSONDecodeError:
        return []


def download_doc_as_text(doc_id: str) -> str | None:
    """Download a Google Doc as plain text. Returns text or None."""
    out_path = f"/tmp/meet_{doc_id}.txt"
    output = run_gog(
        [
            "drive",
            "download",
            doc_id,
            "--format",
            "txt",
            "--out",
            out_path,
            "--account",
            ACCOUNT,
        ]
    )
    try:
        text = Path(out_path).read_text(encoding="utf-8")
        return text
    except (FileNotFoundError, UnicodeDecodeError):
        return None
    finally:
        try:
            Path(out_path).unlink(missing_ok=True)
        except OSError:
            pass


def parse_gemini_notes(text: str) -> dict:
    """Parse a Gemini Notes document into structured sections.

    Returns dict with keys: summary, decisions, next_steps, transcript, attendees.
    """
    result = {
        "attendees": [],
        "summary": "",
        "decisions": [],
        "nextSteps": [],
        "transcript": "",
    }

    # Strip BOM
    text = text.lstrip("\ufeff")

    # Extract attendees from "Invited ..." line
    invited_match = re.search(r"^Invited\s+(.+)$", text, re.MULTILINE)
    if invited_match:
        # Names are space-separated but multi-word — split heuristically
        raw = invited_match.group(1).strip()
        result["attendees"] = _parse_attendee_names(raw)

    # Split on transcript marker
    transcript_marker = re.search(r"📖\s*Transcript", text)
    if transcript_marker:
        header_section = text[: transcript_marker.start()]
        transcript_section = text[transcript_marker.end() :]
    else:
        header_section = text
        transcript_section = ""

    # Extract summary (between "Summary" and "Details")
    summary_match = re.search(
        r"\nSummary\n(.*?)(?:\nDetails\n|\nDecisions\n)",
        header_section,
        re.DOTALL,
    )
    if summary_match:
        summary = summary_match.group(1).strip()
        # Filter out Gemini boilerplate
        if "summary wasn't produced" not in summary:
            result["summary"] = summary

    # Extract decisions (ALIGNED section)
    # Format: "ALIGNED\n" followed by title/description pairs separated by double blank lines
    decisions_section = re.search(
        r"\nALIGNED\n(.*?)(?:\n\n\nMore details:|\n\n\nSuggested next steps|\n\n\nYou should review)",
        header_section,
        re.DOTALL,
    )
    if decisions_section:
        raw = decisions_section.group(1).strip()
        # Split on double blank lines (2+ consecutive newlines)
        blocks = re.split(r"\n\n\n+", raw)
        result["decisions"] = [b.strip() for b in blocks if b.strip()]

    # Extract next steps
    next_steps_match = re.search(
        r"Suggested next steps\n(.*?)(?:\nYou should review|\Z)",
        header_section,
        re.DOTALL,
    )
    if next_steps_match:
        raw_steps = next_steps_match.group(1).strip()
        if "No suggested next steps" not in raw_steps:
            steps = re.findall(r"\*\s*(.+)", raw_steps)
            result["nextSteps"] = [s.strip() for s in steps if s.strip()]

    # Parse transcript — everything after the header lines
    if transcript_section.strip():
        # Skip the date line and title line at the top of transcript
        lines = transcript_section.strip().split("\n")
        # Find first speaker line (format: "Name: text")
        transcript_lines = []
        started = False
        for line in lines:
            if not started and re.match(r"^[A-Z][a-zA-Z\s'-]+:\s", line):
                started = True
            if started:
                transcript_lines.append(line)
        result["transcript"] = "\n".join(transcript_lines)

    return result


def _parse_attendee_names(raw: str) -> list[str]:
    """Parse attendee names from the 'Invited' line.

    Names are space-separated but multi-word. Heuristic: capitalize patterns
    suggest name boundaries. E.g. "Philip D'Agostino Craig Nicholson" →
    ["Philip D'Agostino", "Craig Nicholson"]
    """
    # Split into tokens
    tokens = raw.split()
    names = []
    current = []

    for i, token in enumerate(tokens):
        if not current:
            current.append(token)
            continue

        # A new name starts when:
        # - Token starts with uppercase AND previous token didn't end with apostrophe
        # - AND it's not a common multi-part name connector
        is_name_start = (
            token[0].isupper()
            and token not in ("MHI",)  # known non-name tokens at end
            and not current[-1].endswith("'")
            and len(current) >= 2  # at least first + last name
        )

        if is_name_start:
            names.append(" ".join(current))
            current = [token]
        else:
            current.append(token)

    if current:
        names.append(" ".join(current))

    return names


def extract_meeting_title(doc_name: str) -> str:
    """Extract meeting title from doc name.

    "Sprint Planning - 2026/02/13 09:55 EST - Notes by Gemini"
    → "Sprint Planning"
    """
    parts = doc_name.split(" - ")
    if parts:
        return parts[0].strip()
    return doc_name


def extract_meeting_date(doc_name: str) -> str | None:
    """Extract date from doc name.

    "Sprint Planning - 2026/02/13 09:55 EST - Notes by Gemini"
    → "2026-02-13"
    """
    match = re.search(r"(\d{4})/(\d{2})/(\d{2})", doc_name)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    return None


def match_calendar_event(title: str, date: str | None) -> str | None:
    """Try to match meeting to a calendar event by title + date."""
    if not date:
        return None

    calendar_data = load_json(CALENDAR_PATH)
    meetings = calendar_data.get("meetings", [])

    title_lower = title.lower()
    for meeting in meetings:
        meeting_title = (meeting.get("title") or "").lower()
        meeting_start = meeting.get("start", "")

        # Date must match (start of the ISO date)
        if not meeting_start.startswith(date):
            continue

        # Title fuzzy match — check if calendar title contains meeting name
        # or vice versa (e.g. "Sprint Planning" in "Sprint Planning Meeting")
        if title_lower in meeting_title or meeting_title in title_lower:
            return meeting.get("id")

    return None


def main():
    print(f"[{datetime.now().isoformat()}] Meet transcript sync starting...")

    # Prevent concurrent runs
    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("  Another instance is running, skipping.")
        lock_file.close()
        return

    try:
        _run_sync()
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


def _run_sync():
    # Load existing state and log
    state = load_json(STATE_PATH)
    log = load_json(LOG_PATH)
    if "entries" not in log:
        log["entries"] = {}

    # Fetch docs from Drive
    docs = fetch_gemini_docs()
    print(f"  Found {len(docs)} Gemini Notes documents")

    if not docs:
        return

    new_count = 0
    updated_count = 0

    for doc in docs:
        doc_id = doc.get("id")
        modified_time = doc.get("modifiedTime", "")
        doc_name = doc.get("name", "")

        if not doc_id:
            continue

        # Skip if already synced with same modifiedTime
        if state.get(doc_id) == modified_time:
            continue

        is_update = doc_id in log["entries"]
        action = "Updating" if is_update else "Downloading"
        print(f"  {action}: {doc_name[:70]}")

        # Download and parse
        text = download_doc_as_text(doc_id)
        if text is None:
            print(f"    FAILED to download {doc_id}")
            continue

        parsed = parse_gemini_notes(text)
        title = extract_meeting_title(doc_name)
        date = extract_meeting_date(doc_name)
        calendar_id = match_calendar_event(title, date)

        # Build entry
        entry = {
            "docId": doc_id,
            "title": title,
            "date": date,
            "modifiedTime": modified_time,
            "attendees": parsed["attendees"],
            "summary": parsed["summary"],
            "decisions": parsed["decisions"],
            "nextSteps": parsed["nextSteps"],
            "transcript": parsed["transcript"],
            "calendarEventId": calendar_id,
            "syncedAt": datetime.now().isoformat(),
        }

        log["entries"][doc_id] = entry
        state[doc_id] = modified_time

        if is_update:
            updated_count += 1
        else:
            new_count += 1

    # Save
    log["lastSyncedAt"] = datetime.now().isoformat()
    save_json(log, LOG_PATH)
    save_json(state, STATE_PATH)

    parts = []
    if new_count:
        parts.append(f"{new_count} new")
    if updated_count:
        parts.append(f"{updated_count} updated")
    if not parts:
        parts.append("0 changes")

    print(f"[{datetime.now().isoformat()}] Done. {', '.join(parts)}.")


if __name__ == "__main__":
    main()
