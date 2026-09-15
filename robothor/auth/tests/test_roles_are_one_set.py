"""One set of human roles, and one sentence about each.

``owner | admin | member | user | viewer | auditor`` was written out three
times — here in ``tokens``, in ``cli/user.py`` as ``VALID_ROLES`` and in
``auth/deps.py`` — each copy with a comment asking the next person to keep it in
sync. That is the exact shape of the 2026-08-22 drift incident (#329/#330/#331
were one defect: a hand-maintained list that no longer matched what was
registered), and the failure mode here is worse than a stale list: a role the
token layer accepts and the CLI refuses is an account an operator cannot create,
and a role the CLI accepts and the token layer refuses is an account that cannot
sign in.

The descriptions live beside the set rather than in the UI for the same reason.
A dropdown that explains ``auditor`` differently from the scope table is a
dropdown that will eventually explain it wrongly.
"""

from __future__ import annotations

from robothor.auth import tokens


def test_the_cli_and_the_token_layer_share_one_object():
    """Equality would pass on two frozensets that agree today. Identity is what
    makes a divergence impossible rather than merely unlikely."""
    from robothor.cli.user import VALID_ROLES

    assert VALID_ROLES is tokens.HUMAN_ROLES


def test_the_auth_deps_copy_is_the_same_object_too():
    from robothor.auth import deps

    assert deps._HUMAN_ROLES is tokens.HUMAN_ROLES


def test_the_private_name_still_answers_for_its_existing_callers():
    assert tokens._HUMAN_ROLES is tokens.HUMAN_ROLES


def test_every_role_has_a_description():
    """A role with no sentence would reach the Helm's role picker as a bare
    identifier, which is how an operator picks ``auditor`` for somebody who
    needed ``member``."""
    assert set(tokens.ROLE_DESCRIPTIONS) == set(tokens.HUMAN_ROLES)
    assert all(text.strip() for text in tokens.ROLE_DESCRIPTIONS.values())


def test_every_role_resolves_to_scopes():
    """The set and the scope table are two halves of the same fact. A role in
    one and not the other raises at issue time, on somebody's first sign-in."""
    for role in tokens.HUMAN_ROLES:
        assert tokens._default_scopes("user", role)
