"""A member's notification preferences for a workspace: one parser, one stored form.

The bridge's ``PUT /api/rooms/{id}/notify`` stores ``room_members.notify`` as
``{"in_app": bool, "muted": bool, "channels": {<name>: bool}}``; fan-out reads
the same through ``robothor.rooms.notify``. A missing or empty value means the
defaults: in-app on, not muted, no external channel.
"""

from __future__ import annotations

from robothor.rooms import notify


def test_the_bridges_stored_form_parses():
    prefs = notify.parse_notify(
        {"in_app": False, "muted": False, "channels": {"telegram": True, "slack": False}}
    )
    assert (prefs.in_app, prefs.muted, prefs.channels) == (False, False, ("telegram",))


def test_missing_or_empty_is_the_defaults():
    for raw in (None, {}, {"channels": None}):
        prefs = notify.parse_notify(raw)
        assert (prefs.in_app, prefs.muted, prefs.channels) == (True, False, ())


def test_the_stored_form_round_trips():
    stored = notify.stored_notify(in_app=False, muted=True, channels={"telegram": True})
    assert stored == {"in_app": False, "muted": True, "channels": {"telegram": True}}
    prefs = notify.parse_notify(stored)
    assert (prefs.in_app, prefs.muted, prefs.channels) == (False, True, ("telegram",))


def test_a_room_that_notifies_nobody_by_default_mutes_a_member_who_said_nothing():
    assert notify.parse_notify({}, default_muted=True).muted is True
    assert notify.parse_notify({"muted": False}, default_muted=True).muted is False


def test_only_true_channels_with_names_count():
    prefs = notify.parse_notify({"channels": {"telegram": 1, "": True, "email": "yes", "x": True}})
    assert prefs.channels == ("x",)


def test_nothing_is_delivered_when_muted():
    prefs = notify.parse_notify({"muted": True, "in_app": True, "channels": {"telegram": True}})
    assert prefs.delivers_in_app is False
    assert prefs.external_channels == ()
    assert notify.parse_notify({"channels": {"telegram": True}}).external_channels == ("telegram",)
