"""Environment the platform hands to third-party libraries before they load.

Not configuration of Genus — configuration of what a dependency does at
import time, which is why it lives beside the settings model rather than in
it: the settings model is read after imports, and this has to be in place
before them.

LiteLLM 1.101.0 (2026-09-14) started fetching its model price list from
GitHub when imported. Every process that imports litellm — the engine at
boot, the bridge validating a manifest, a test — then depended on an
outbound HTTP call it never asked for; in CI, where the bridge suite refuses
network egress, the manifest validator's lazy import of ``llm_client`` died
with a swallowed AssertionError and every create answered 422. The bundled
map is the one we test against.
"""

from __future__ import annotations

import os

#: LiteLLM reads this at import; "True" means "use the bundled price list,
#: never the network". Anything else set by the operator is left alone.
LITELLM_OFFLINE_FLAG = "LITELLM_LOCAL_MODEL_COST_MAP"


def pin_litellm_offline() -> None:
    """Make LiteLLM use its bundled price list unless the operator chose otherwise."""
    os.environ.setdefault(LITELLM_OFFLINE_FLAG, "True")
