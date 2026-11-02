"""The capability copy learns of credential intake from the vault handler's marker.

The credential-intake slice's vault handler declares ``CREDENTIAL_INTAKE = True``
for exactly this probe. The honest-copy slice probed a private helper instead
(``_intake_gate``), which ties the prompt's wording to an internal name. The
probe reads the public marker: present and true means this build has intake.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from robothor.engine import capabilities
from robothor.engine.tools.handlers import vault

if TYPE_CHECKING:
    import pytest


def test_the_marker_alone_means_intake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vault, "CREDENTIAL_INTAKE", True, raising=False)
    monkeypatch.delattr(vault, "_intake_gate", raising=False)
    assert capabilities.credential_intake() is True


def test_no_marker_means_no_intake_whatever_the_handler_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(vault, "CREDENTIAL_INTAKE", raising=False)
    monkeypatch.setattr(vault, "_intake_gate", lambda ctx: None, raising=False)
    assert capabilities.credential_intake() is False


def test_this_build_declares_it() -> None:
    assert vault.CREDENTIAL_INTAKE is True
    assert capabilities.credential_intake() is True
