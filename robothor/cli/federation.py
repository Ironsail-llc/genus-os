"""Federation (peer-to-peer instance networking) commands."""

from __future__ import annotations

import argparse  # noqa: TC003
from typing import Any


def _invert_rel(r: str | Any) -> str:
    """Invert relationship for display."""
    s = r.value if hasattr(r, "value") else str(r)
    if s == "parent":
        return "child"
    if s == "child":
        return "parent"
    return "peer"


def cmd_federation(args: argparse.Namespace) -> int:
    sub = getattr(args, "federation_command", None)

    if sub == "init":
        from robothor.federation.config import FederationConfig
        from robothor.federation.identity import init_identity

        config = FederationConfig.from_env()
        instance = init_identity(config)
        print(f"Instance ID:   {instance.id}")
        print(f"Display name:  {instance.display_name}")
        print(f"Identity file: {config.identity_file}")
        return 0

    if sub == "invite":
        from robothor.federation.config import FederationConfig
        from robothor.federation.identity import create_invite_token
        from robothor.federation.models import Relationship

        config = FederationConfig.from_env()
        relationship = Relationship(args.relationship)
        token = create_invite_token(config, relationship=relationship, ttl_hours=args.ttl)
        print(f"Invite token (expires in {args.ttl}h):\n")
        print(token.token)
        print(f"\nRelationship: {relationship.value}")
        print(f"Peer sees you as: {_invert_rel(relationship)}")
        return 0

    if sub == "connect":
        from robothor.federation.config import FederationConfig
        from robothor.federation.connections import save_connection
        from robothor.federation.identity import consume_invite_token

        config = FederationConfig.from_env()
        try:
            connection = consume_invite_token(config, args.token, trust=args.trust)
            save_connection(connection)
            print(f"Connected to: {connection.peer_name}")
            print(f"Connection ID: {connection.id}")
            print(f"Relationship:  {connection.relationship.value}")
            print(f"State:         {connection.state.value}")
            print(f"\nExports: {', '.join(connection.exports) or 'none'}")
            print(f"Imports: {', '.join(connection.imports) or 'none'}")
            return 0
        except ValueError as e:
            print(f"Error: {e}")
            return 1
        except RuntimeError as e:
            print(f"Error: {e}")
            return 1

    if sub == "status":
        from robothor.federation.config import FederationConfig
        from robothor.federation.connections import load_connections
        from robothor.federation.identity import get_identity

        config = FederationConfig.from_env()
        identity = get_identity(config)
        if not identity:
            print("No instance identity. Run: robothor federation init")
            return 1
        print(f"Instance: {identity.display_name} ({identity.id[:12]}...)")
        print()
        connections = load_connections()
        if not connections:
            print("No connections.")
            return 0
        for conn in connections:
            print(
                f"  {conn.peer_name:<24} {conn.state.value:<12} {conn.relationship.value:<8} {conn.id[:12]}..."
            )
            if conn.exports:
                print(f"    exports: {', '.join(conn.exports)}")
            if conn.imports:
                print(f"    imports: {', '.join(conn.imports)}")
        print(f"\n{len(connections)} connection(s)")
        return 0

    if sub == "list":
        from robothor.federation.connections import load_connections

        connections = load_connections()
        if not connections:
            print("No connections.")
            return 0
        for conn in connections:
            print(
                f"{conn.id[:12]}  {conn.peer_name:<24} {conn.state.value:<12} {conn.relationship.value}"
            )
        return 0

    if sub == "export":
        from robothor.federation.connections import (
            ConnectionManager,
            load_connections,
            save_connection,
        )

        mgr = ConnectionManager()
        for conn in load_connections():
            mgr.add(conn)
        try:
            conn = mgr.add_export(args.connection, args.capability)
            save_connection(conn)
            print(f"Exported '{args.capability}' to {conn.peer_name}")
            return 0
        except ValueError as e:
            # Try partial ID match
            for c in mgr.list_all():
                if c.id.startswith(args.connection):
                    conn = mgr.add_export(c.id, args.capability)
                    save_connection(conn)
                    print(f"Exported '{args.capability}' to {conn.peer_name}")
                    return 0
            print(f"Error: {e}")
            return 1

    if sub == "suspend":
        from robothor.federation.connections import (
            ConnectionManager,
            load_connections,
            save_connection,
        )

        mgr = ConnectionManager()
        for conn in load_connections():
            mgr.add(conn)
        try:
            conn = mgr.suspend(args.connection)
            save_connection(conn)
            print(f"Suspended connection to {conn.peer_name}")
            return 0
        except ValueError as e:
            print(f"Error: {e}")
            return 1

    if sub == "remove":
        from robothor.federation.connections import delete_connection, load_connections

        connections = load_connections()
        for conn in connections:
            if conn.id == args.connection or conn.id.startswith(args.connection):
                if delete_connection(conn.id):
                    print(f"Removed connection to {conn.peer_name}")
                    return 0
                print("Error: Failed to delete connection")
                return 1
        print(f"Error: Connection not found: {args.connection}")
        return 1

    print("Usage: robothor federation {init|invite|connect|status|list|export|suspend|remove}")
    return 0
