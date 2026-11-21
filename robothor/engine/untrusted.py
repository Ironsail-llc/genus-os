"""The untrusted-content boundary that external text reaches the model inside.

One helper, used by the session (external tool output) and by hooks (event
payloads), so both neutralise the boundary's closing tag the same way.
"""

from __future__ import annotations

import re

__all__ = ["neutralise_boundary"]

_BOUNDARY_CLOSE = re.compile(r"</\s*(untrusted_content)", re.IGNORECASE)


def neutralise_boundary(text: str) -> str:
    """Stop external text from closing the untrusted-content boundary early."""
    return _BOUNDARY_CLOSE.sub(r"<\\/\1", text)
