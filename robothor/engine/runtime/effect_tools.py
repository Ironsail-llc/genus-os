"""What a write tool acts on, and how its uncertain outcome is read back."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class EffectSpec:
    target: tuple[str, ...] | None = None
    fence: str = "target"
    verify: Callable[[dict[str, Any]], Any] | None = None


def plugin_specs() -> dict[str, EffectSpec]:
    return {}


def recover(scope: Any, effect_id: Any) -> dict[str, Any] | None:
    raise NotImplementedError


def has_verifier(tool_name: str) -> bool:
    raise NotImplementedError
