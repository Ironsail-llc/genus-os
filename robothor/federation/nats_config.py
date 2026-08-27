"""Generate the NATS server config that makes the asymmetry structural.

Configuration is not enforcement, and application checks are not a boundary. A
child that can address the parent's responder at all is one application bug
away from reaching it; a child whose account has no import for that service
cannot form the request in the first place, and the broker logs the attempt.

The shape, for one connection:

    accounts {
      ENGINE {                       # where our responder listens
        users: [ engine ]
        exports: [ {service: robothor.<id>.command, accounts: [FED_<id>]} ]
        jetstream: enabled
      }
      FED_<id> {                     # the peer, and nothing else
        users: [ fed_<id> ]
        imports: [ {service: {account: ENGINE, subject: robothor.<id>.command}} ]
      }
    }

What that buys, none of which depends on the application behaving:

  - the peer can reach exactly one subject, the one we exported to it
  - the peer cannot reach ANOTHER peer: separate accounts, no shared subjects
  - the peer cannot reach JetStream: no `jetstream` key on its account, so the
    1 GB file store is not addressable from it
  - the peer cannot subscribe to our engine's internal traffic

This file exists partly because `/etc/nats/nats-server.conf` was untracked:
`robothor-nats.service` pointed at a path nothing in the repo produced, so a
rebuild from source lost the config — and the config it lost contained an
unauthenticated `leafnodes { listen: 0.0.0.0:7422 }` binding every interface
into the same global account as the engine.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field

#: The account our own engine connects with. Everything federation-facing is
#: exported FROM here, never into it.
ENGINE_ACCOUNT = "ENGINE"

_SAFE = re.compile(r"[^A-Za-z0-9]")


def account_name(connection_id: str) -> str:
    """A NATS-safe account name for one connection.

    Connection ids are UUIDs; dashes are not safe in an account name, and the
    name has to be stable because it is written into both the server config and
    the connection row.
    """
    return f"FED_{_SAFE.sub('_', connection_id).upper()}"


def user_name(connection_id: str) -> str:
    return f"fed_{_SAFE.sub('_', connection_id).lower()}"


def command_subject(connection_id: str) -> str:
    return f"robothor.{connection_id}.command"


@dataclass
class PeerAccount:
    """One peer's account: a credential and exactly one importable service."""

    connection_id: str
    password: str = field(default_factory=lambda: secrets.token_urlsafe(24))

    @property
    def account(self) -> str:
        return account_name(self.connection_id)

    @property
    def user(self) -> str:
        return user_name(self.connection_id)

    @property
    def subject(self) -> str:
        return command_subject(self.connection_id)


def render_config(
    *,
    listen: str = "127.0.0.1:4222",
    engine_password: str,
    peers: list[PeerAccount],
    jetstream_dir: str = "",
    http_listen: str = "",
) -> str:
    """Render a complete nats-server config.

    Deliberately absent: any `leafnodes` block. Nothing uses leaf nodes —
    federation dials in as a client, into a per-connection account, which gets
    the same isolation without an unauthenticated remote administrative
    interface to the message store.
    """
    exports = "\n".join(
        f'        {{ service: "{p.subject}", accounts: [{p.account}] }}' for p in peers
    )
    engine_block = [
        f"  {ENGINE_ACCOUNT}: {{",
        f'    users: [ {{ user: "engine", password: "{engine_password}" }} ]',
    ]
    if exports:
        engine_block += ["    exports: [", exports, "    ]"]
    if jetstream_dir:
        engine_block += ["    jetstream: enabled"]
    engine_block += ["  }"]

    peer_blocks = []
    for p in peers:
        peer_blocks += [
            f"  {p.account}: {{",
            "    users: [",
            "      {",
            f'        user: "{p.user}", password: "{p.password}",',
            # Per-user permissions on top of account isolation. The account
            # already makes other connections unroutable, but "unroutable"
            # produces no evidence — a request simply finds no responder, which
            # looks identical to a peer that never tried. With an explicit
            # allow-list the broker REFUSES and logs a Permissions Violation,
            # so an attempt to reach a sibling connection is visible in
            # nats-server.log rather than silent.
            "        permissions: {",
            f'          publish: {{ allow: ["{p.subject}"] }}',
            '          subscribe: { allow: ["_INBOX.>"] }',
            "        }",
            "      }",
            "    ]",
            "    imports: [",
            f'      {{ service: {{ account: {ENGINE_ACCOUNT}, subject: "{p.subject}" }} }}',
            "    ]",
            # No `jetstream` key. A peer account cannot address $JS.API.>.
            "  }",
        ]

    lines = [
        "# Generated by robothor.federation.nats_config — do not hand-edit.",
        "# Regenerate with `robothor federation invite` / `federation accept`.",
        f"listen: {listen}",
    ]
    if http_listen:
        lines.append(f"http: {http_listen}")
    if jetstream_dir:
        lines += ["jetstream {", f'  store_dir: "{jetstream_dir}"', "  max_file_store: 1GB", "}"]
    lines += ["", "accounts {", *engine_block, *peer_blocks, "}", ""]
    return "\n".join(lines)
