"""Show a dotenv file with its credentials masked (GEN-42).

``read_file`` refuses every secrets file by name (:mod:`secret_paths`), and
for most of them that is the whole answer. The instance env file is the
exception that hurt: it is where the operator's SETTINGS live, beside their
credentials, so refusing it left the assistant unable to say what its own
log level, tenant or model is. This module returns such a file with every key
and every non-secret value visible and every secret value reduced to ``****``
plus its last four characters -- enough to tell two tokens apart, never
enough to use one.

A value is secret when EITHER its key says so or the value does:

* **the key** -- a declared setting marked ``secret`` in the settings
  registry; a name the shared redactor already treats as a credential
  (:func:`robothor.secrets.redaction._argument_reads_as_a_credential`); or a
  name containing a credential word (``TOKEN``, ``SECRET``, ``PASSWORD``,
  ``KEY``, ``AUTH``, ``COOKIE``, ``PRIVATE``, ``DSN`` ...). Over-masking a
  setting costs a less useful answer; under-masking costs a credential.
* **the value** -- anything :func:`robothor.secrets.redaction.redact` or
  :func:`robothor.engine.guardrails.redact_secrets` would change: vendor
  token shapes, ``sk-`` keys, a URL carrying ``user:password@``.

Comments are free text and can hold a credential no rule recognises, so a
comment is shown only when it is a commented-out ``KEY=VALUE`` assignment
(masked by the same rules as a live one); any other comment text is replaced
by ``# [comment hidden]``.

Fails closed: a body that is not plain ``KEY=VALUE`` lines (a pasted PEM, a
multi-line quoted value) or that is SOPS-encrypted (``ENC[...]`` values, a
``sops_*`` key) returns ``None``, and the caller refuses the read.
"""

from __future__ import annotations

import re

__all__ = [
    "COMMENT_PLACEHOLDER",
    "MASK",
    "key_is_secret",
    "mask_dotenv",
    "mask_value",
    "value_is_secret",
]

MASK = "****"

#: Shorter than this, even the last four characters are too much of the value.
_MIN_LEN_FOR_TAIL = 12

#: Credential words that may appear anywhere in a name, glued or not
#: (``PGPASSWORD``, ``GHTOKEN``).
_CREDENTIAL_SUBSTRINGS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "PASSPHRASE",
    "CREDENTIAL",
    "APIKEY",
    "COOKIE",
    "PRIVATE",
)

#: Short credential words that count only as a whole ``_``-separated segment:
#: ``KEY`` in ``ACME_API_KEY`` but not in ``MONKEY_MODE``.
_CREDENTIAL_SEGMENTS = frozenset(
    {"KEY", "KEYS", "PASS", "PWD", "AUTH", "DSN", "SALT", "PAT", "JWT", "WEBHOOK", "SIGNATURE"}
)

_LINE = re.compile(
    r"^(?P<lead>\s*(?:export\s+)?)(?P<key>[A-Za-z_][A-Za-z0-9_.]*)(?P<sep>\s*=\s*)(?P<val>.*)$"
)


def mask_value(value: str) -> str:
    """``****`` + the last four characters, or ``****`` alone for a short value."""
    if not value:
        return ""
    if len(value) < _MIN_LEN_FOR_TAIL:
        return MASK
    return MASK + value[-4:]


def _declared_secret(key: str) -> bool:
    try:
        from robothor.settings.registry import field_index

        row = field_index().get(key)
    except Exception:  # noqa: BLE001 - a registry that will not load is not evidence of "public"
        return False
    return bool(row and row.get("secret"))


def key_is_secret(key: str) -> bool:
    """Whether the NAME on the left of ``=`` says the value is a credential."""
    upper = key.upper()
    if any(word in upper for word in _CREDENTIAL_SUBSTRINGS):
        return True
    if any(seg in _CREDENTIAL_SEGMENTS for seg in re.split(r"[_.\-]+", upper)):
        return True
    from robothor.secrets.redaction import _argument_reads_as_a_credential

    if _argument_reads_as_a_credential(key):
        return True
    return _declared_secret(key)


def value_is_secret(value: str) -> bool:
    """Whether the VALUE has a credential's shape, whatever it is called."""
    if not value:
        return False
    from robothor.engine.guardrails import redact_secrets
    from robothor.secrets.redaction import redact

    return redact(value) != value or redact_secrets(value) != value


#: A right-hand side split into its value and a same-line ``# comment``. A
#: ``#`` only starts a comment after whitespace, as in dotenv.
_INLINE_COMMENT = re.compile(r"""^(?P<val>"[^"]*"|'[^']*'|.*?)(?P<tail>\s+#.*)?$""", re.DOTALL)


def _split_inline_comment(raw: str) -> tuple[str, bool]:
    """``(value part, had a comment)`` for a raw right-hand side."""
    match = _INLINE_COMMENT.match(raw.strip())
    if match is None:  # pragma: no cover - the pattern matches any string
        return raw, False
    return match.group("val"), bool(match.group("tail"))


def _unquote(raw: str) -> tuple[str, str]:
    """``(value, quote)`` for a raw right-hand side; an unquoted one is stripped."""
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1], text[0]
    return text, ""


def _is_sops(text: str) -> bool:
    if "ENC[" in text:
        return True
    return any(
        (m := _LINE.match(line)) is not None and m.group("key").lower().startswith("sops_")
        for line in text.splitlines()
    )


#: What a comment that is not a commented-out assignment is shown as.
COMMENT_PLACEHOLDER = "# [comment hidden]"


def _scrub_comment(line: str) -> str:
    """A commented-out ``KEY=VALUE`` masked like a live one; any other comment hidden.

    Comments are free text, and free text is where an operator leaves
    ``# db pw is hunter2, rotate in Q3`` -- which no name or shape rule can
    recognise. So only a comment that parses as ONE assignment with a single
    token value (quoted or not) is shown; everything else becomes the placeholder.
    """
    body = line.lstrip().lstrip("#").strip()
    match = _LINE.match(body)
    if match is None:
        return COMMENT_PLACEHOLDER
    key = match.group("key")
    raw = match.group("val").strip()
    value, quote = _unquote(raw)
    if any(ch.isspace() for ch in value) or (not quote and raw[:1] in ("'", '"')):
        # Prose after an ``=``, quoted or not (``# note="the pw is ..."``), or
        # an unclosed quote. A commented-out setting with spaces is rare.
        return COMMENT_PLACEHOLDER
    if key_is_secret(key) or value_is_secret(value):
        shown = mask_value(value)
    else:
        shown = value
    return f"# {key}={quote}{shown}{quote}" if value else f"# {key}="


def mask_dotenv(text: str) -> str | None:
    """*text* with every secret value masked, or ``None`` when it must be refused."""
    if _is_sops(text):
        return None
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body) :]
        stripped = body.strip()
        if not stripped:
            out.append(line)
            continue
        if stripped.startswith("#"):
            out.append(_scrub_comment(body) + ending)
            continue
        match = _LINE.match(body)
        if match is None:
            return None
        key = match.group("key")
        raw, commented = _split_inline_comment(match.group("val"))
        value, quote = _unquote(raw)
        # A same-line comment is free text, hidden like a whole-line one.
        suffix = f" {COMMENT_PLACEHOLDER}" if commented else ""
        opening = raw.strip()[:1]
        if not quote and opening and opening in "\"'":
            # An opening quote with no closing one: a multi-line value whose
            # continuation lines would be shown unparsed.
            return None
        if key_is_secret(key) or value_is_secret(value):
            shown = f"{quote}{mask_value(value)}{quote}" if value else raw.strip()
            out.append(f"{match.group('lead')}{key}{match.group('sep')}{shown}{suffix}{ending}")
        elif commented:
            out.append(
                f"{match.group('lead')}{key}{match.group('sep')}{raw.strip()}{suffix}{ending}"
            )
        else:
            out.append(line)
    return "".join(out)
