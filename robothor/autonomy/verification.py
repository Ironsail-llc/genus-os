"""Extract one authenticated, fresh website verification without model exposure."""

import re
from datetime import UTC, datetime
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

#: Mail domains that unrelated senders share. DMARC alignment proves only that
#: a message really came from the domain in its From header, so authorising a
#: shared one authorises everybody who has an account there: with gmail.com
#: allowed for a website, any Gmail user is that website's verification. A
#: merchant that sends through an email service still signs as its own
#: single-tenant domain (mail.acme-shop.example), and that is what belongs
#: here. Subdomains are covered too: an address handed out by the provider is
#: still the provider's namespace, not the website's.
SHARED_MAIL_DOMAINS = frozenset(
    {
        # Consumer mailboxes.
        "aol.com",
        "gmail.com",
        "gmx.com",
        "gmx.net",
        "googlemail.com",
        "hotmail.com",
        "hushmail.com",
        "icloud.com",
        "live.com",
        "mac.com",
        "mail.com",
        "mail.ru",
        "me.com",
        "msn.com",
        "naver.com",
        "outlook.com",
        "pm.me",
        "proton.me",
        "protonmail.ch",
        "protonmail.com",
        "qq.com",
        "rocketmail.com",
        "tuta.com",
        "tutanota.com",
        "web.de",
        "yahoo.com",
        "yandex.com",
        "yandex.ru",
        "ymail.com",
        "zoho.com",
        "zohomail.com",
        # Bulk and transactional senders whose own domains carry many customers.
        "amazonses.com",
        "brevo.com",
        "ccsend.com",
        "constantcontact.com",
        "createsend.com",
        "elasticemail.com",
        "klaviyomail.com",
        "mailchimp.com",
        "mailerlite.com",
        "mailgun.net",
        "mailgun.org",
        "mailjet.com",
        "mandrillapp.com",
        "mcsv.net",
        "mtasv.net",
        "postmarkapp.com",
        "rsgsv.net",
        "sendgrid.net",
        "sendinblue.com",
        "sendpulse.com",
        "smtp2go.com",
        "sparkpostmail.com",
        "sparkpostmail1.com",
    }
)


def shared_mail_domain(domain: str) -> bool:
    """A shared provider's own namespace, apex or subdomain."""
    domain = domain.strip().strip(".").lower()
    return any(domain == shared or domain.endswith("." + shared) for shared in SHARED_MAIL_DOMAINS)


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
    # Authority written before shared providers were refused, or restored from
    # an older grant, is not a reason to accept one now.
    if (
        domain != host
        and not domain.endswith("." + host)
        and (domain not in sender_domains or shared_mail_domain(domain))
    ):
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
