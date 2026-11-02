"""The chart's hooks ConfigMap holds a webhooks.yaml the router's loader accepts.

Renders ``templates/hooks/configmap.yaml`` with ``helm template`` and loads the
result with :func:`robothor.engine.webhooks.load_webhook_config`, so a change to
the template that the loader would reject (or silently read as no channels)
fails here rather than in a running pod. Skipped where helm or the chart's
dependency archives are not available.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from robothor.engine.webhooks import load_webhook_config

CHART = Path(__file__).resolve().parents[1] / "helm" / "genus-os"
TEMPLATE = "templates/hooks/configmap.yaml"

pytestmark = [
    pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed"),
    pytest.mark.skipif(
        not any((CHART / "charts").glob("*.tgz")),
        reason="chart dependencies not built (helm dependency build helm/genus-os)",
    ),
]

CHANNELS = """
hooks:
  enabled: true
  channels:
    example:
      stream: example-events
      secret_env: EXAMPLE_WEBHOOK_SECRET
      signature_scheme: hmac_ts_hex
      signature_header: X-Example-Signature
      timestamp_header: X-Example-Timestamp
      delivery_id_header: X-Example-Delivery
      event_type_field: type
      event_type_prefix: example.
      rate_limit_per_min: 120
"""


def _render(tmp_path: Path, values: str) -> dict:
    values_file = tmp_path / "values.yaml"
    values_file.write_text(values)
    out = subprocess.run(
        [
            "helm",
            "template",
            "gos",
            str(CHART),
            "--namespace",
            "genus",
            "--values",
            str(values_file),
            "--show-only",
            TEMPLATE,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    return yaml.safe_load(out.stdout)


def _load(tmp_path: Path, configmap: dict):
    config_file = tmp_path / "webhooks" / "webhooks.yaml"
    config_file.parent.mkdir()
    config_file.write_text(configmap["data"]["webhooks.yaml"])
    return load_webhook_config(config_file)


def test_rendered_channel_round_trips_through_the_loader(tmp_path: Path) -> None:
    configmap = _render(tmp_path, CHANNELS)

    assert configmap["kind"] == "ConfigMap"
    assert configmap["metadata"]["name"] == "gos-genus-os-hooks-webhooks"

    config = _load(tmp_path, configmap)

    assert list(config.channels) == ["example"]
    channel = config.channels["example"]
    assert channel.stream == "example-events"
    assert channel.secret_env == "EXAMPLE_WEBHOOK_SECRET"
    assert channel.signature_scheme == "hmac_ts_hex"
    assert channel.signature_header == "X-Example-Signature"
    assert channel.timestamp_header == "X-Example-Timestamp"
    assert channel.delivery_id_header == "X-Example-Delivery"
    assert channel.event_type_field == "type"
    assert channel.event_type_prefix == "example."
    assert channel.rate_limit_per_min == 120
    assert channel.enabled is True


def test_empty_channel_map_loads_as_no_channels(tmp_path: Path) -> None:
    configmap = _render(tmp_path, "hooks:\n  enabled: true\n")

    assert _load(tmp_path, configmap).channels == {}
