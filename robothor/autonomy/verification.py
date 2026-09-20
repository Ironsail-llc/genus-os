"""Extract one authenticated, fresh website verification without model exposure."""

import re
from datetime import UTC, datetime
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit


def extract_verification(
    message: dict[str, Any],
    *,
    recipient: str,
    destination: str,
    after: int,
    mode: str,
    sender_domains: frozenset[str] = frozenset(),
) -> str | None:
    host = urlsplit(destination).hostname or ""
    headers = {}
    for item in message.get("payload", {}).get("headers", []):
        name = item["name"].lower()
        if name in headers and name in {"from", "to", "date", "authentication-results"}:
            return None
        headers[name] = item["value"]
    sender = getaddresses([headers.get("from", "")])
    recipients = {address.lower() for _, address in getaddresses([headers.get("to", "")])}
    if len(sender) != 1 or recipient.lower() not in recipients:
        return None
    domain = sender[0][1].rsplit("@", 1)[-1].lower()
    if domain != host and not domain.endswith("." + host) and domain not in sender_domains:
        return None
    authentication = headers.get("authentication-results", "").lower()
    # This adapter reads Gmail, whose receiving MTA adds this header. A
    # sender-supplied result from another authentication service is not proof.
    if not authentication.lstrip().startswith("mx.google.com;"):
        return None
    if not re.search(
        r"\bdmarc=pass\b[^;]*\bheader\.from=" + re.escape(domain) + r"(?:[;\s]|$)", authentication
    ):
        return None
    try:
        stamp = int(message.get("internalDate", "0")) // 1000
        sent = parsedate_to_datetime(headers.get("date", "")).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None
    now = datetime.now(UTC).timestamp()
    if not after <= stamp <= now + 60 or sent < after - 60 or now - stamp > 600:
        return None
    from robothor.engine.tools.handlers.gws import _extract_body

    text = _extract_body(message.get("payload", {}))
    if mode == "code":
        codes = set(re.findall(r"(?<!\d)\d{6}(?!\d)", text))
        return codes.pop() if len(codes) == 1 else None
    if mode == "link":
        links = {
            url
            for url in re.findall(r'https://[^\s<>"\)]+', text)
            if urlsplit(url).hostname == host and not urlsplit(url).username
        }
        return links.pop() if len(links) == 1 else None
    raise ValueError("unsupported_verification_mode")
