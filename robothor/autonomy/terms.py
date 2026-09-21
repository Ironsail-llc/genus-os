"""Unambiguous merchant billing labels shared by inspection and execution."""

from datetime import UTC, date, datetime


def interval_months(text: str) -> int | None:
    label = " ".join(text.strip().lower().split())
    periods = {
        "monthly": 1,
        "every month": 1,
        "quarterly": 3,
        "every quarter": 3,
        "annually": 12,
        "annual": 12,
        "yearly": 12,
        "every year": 12,
    }
    for months in (1, 2, 3, 6, 12):
        periods[f"every {months} month" + ("s" if months != 1 else "")] = months
    return periods.get(label)


def renewal_date(text: str) -> date | None:
    value = text.strip()
    try:
        return date.fromisoformat(value)
    except ValueError:
        for pattern in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"):
            try:
                return datetime.strptime(value, pattern).replace(tzinfo=UTC).date()
            except ValueError:
                continue
    return None
