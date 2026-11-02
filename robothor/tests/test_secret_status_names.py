"""Which environment names the Secrets table calls credentials.

The table unions declared secrets with names that LOOK like credentials. The
shape match is what finds a third-party ``GITHUB_TOKEN`` nobody declared, but it
also matched ``ROBOTHOR_MAIN_SESSION_KEY``, a declared setting whose name
happens to end in "key". A name the settings model declares and does
not mark secret is not a credential, whatever it is called.
"""

from __future__ import annotations


def test_a_declared_non_secret_setting_is_not_listed(monkeypatch):
    from robothor.secrets.status import environment_credential_names

    monkeypatch.setenv("ROBOTHOR_MAIN_SESSION_KEY", "agent:main:primary")
    listed = "ROBOTHOR_MAIN_SESSION_KEY" in environment_credential_names()
    assert not listed, "a declared non-secret setting was listed as a credential"


def test_an_undeclared_credential_shaped_name_is_still_listed(monkeypatch):
    from robothor.secrets.status import environment_credential_names

    monkeypatch.setenv("EXAMPLE_VENDOR_API_TOKEN", "placeholder")
    listed = "EXAMPLE_VENDOR_API_TOKEN" in environment_credential_names()
    assert listed, "an undeclared credential-shaped name dropped out of the table"
