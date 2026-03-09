"""Federation configuration — loaded from env vars and config files."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FederationConfig:
    """Federation configuration for this instance."""

    # Instance identity
    instance_id: str = ""
    instance_name: str = ""

    # NATS
    nats_url: str = "nats://127.0.0.1:4222"
    nats_enabled: bool = False

    # Endpoint that peers use to reach this instance
    public_endpoint: str = ""

    # Paths
    config_dir: Path = field(default_factory=lambda: Path.home() / "robothor" / ".robothor")
    identity_file: Path = field(
        default_factory=lambda: Path.home() / "robothor" / ".robothor" / "identity.json"
    )

    @classmethod
    def from_env(cls) -> FederationConfig:
        """Load from environment variables and config files."""
        workspace = Path(os.environ.get("ROBOTHOR_WORKSPACE", Path.home() / "robothor"))
        config_dir = workspace / ".robothor"

        # Try to load from config file
        config_path = config_dir / "federation.yaml"
        file_config: dict[str, Any] = {}
        if config_path.exists():
            try:
                with config_path.open() as f:
                    file_config = yaml.safe_load(f) or {}
            except Exception as e:
                logger.warning("Failed to load federation config: %s", e)

        return cls(
            instance_id=os.environ.get("ROBOTHOR_INSTANCE_ID", file_config.get("instance_id", "")),
            instance_name=os.environ.get(
                "ROBOTHOR_INSTANCE_NAME", file_config.get("instance_name", "")
            ),
            nats_url=os.environ.get(
                "ROBOTHOR_NATS_URL", file_config.get("nats_url", "nats://127.0.0.1:4222")
            ),
            nats_enabled=_str_to_bool(
                os.environ.get("ROBOTHOR_NATS_ENABLED", str(file_config.get("nats_enabled", False)))
            ),
            public_endpoint=os.environ.get(
                "ROBOTHOR_PUBLIC_ENDPOINT", file_config.get("public_endpoint", "")
            ),
            config_dir=config_dir,
            identity_file=config_dir / "identity.json",
        )

    def save(self) -> None:
        """Persist current config to federation.yaml."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        config_path = self.config_dir / "federation.yaml"
        data = {
            "instance_id": self.instance_id,
            "instance_name": self.instance_name,
            "nats_url": self.nats_url,
            "nats_enabled": self.nats_enabled,
            "public_endpoint": self.public_endpoint,
        }
        with config_path.open("w") as f:
            yaml.safe_dump(data, f, default_flow_style=False)


def _str_to_bool(s: str) -> bool:
    return s.lower() in ("true", "1", "yes")


def load_identity(config: FederationConfig) -> dict[str, Any] | None:
    """Load identity from the identity file."""
    if not config.identity_file.exists():
        return None
    try:
        result: dict[str, Any] = json.loads(config.identity_file.read_text())
        return result
    except Exception as e:
        logger.warning("Failed to load identity: %s", e)
        return None


def save_identity(config: FederationConfig, identity: dict[str, Any]) -> None:
    """Save identity to the identity file."""
    config.config_dir.mkdir(parents=True, exist_ok=True)
    config.identity_file.write_text(json.dumps(identity, indent=2))
