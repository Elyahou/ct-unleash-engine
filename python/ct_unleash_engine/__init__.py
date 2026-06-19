"""Drop-in fast feature-flag engine backed by a PyO3 binding to yggdrasil-core.

``UnleashEngine`` matches the surface the Unleash Python SDK calls on its
engine (``take_state``, ``get_state``, ``is_enabled``, ``get_variant``,
``count_toggle``, ``count_variant``, ``get_metrics``,
``should_emit_impression_event``, ``list_known_toggles``), so it can be used as
a drop-in replacement for ``yggdrasil_engine.engine.UnleashEngine`` or shadowed
alongside it.

The compiled ``_native`` extension does the evaluation reading the context
directly from the Python dict (no JSON on the hot path). This thin Python layer
only adds the dataclass shapes the SDK expects and JSON-decodes the rare
metadata calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ._native import NativeEngine, __core_version__

__all__ = ["UnleashEngine", "Variant", "__core_version__"]


@dataclass
class Variant:
    """Variant result, shaped to match the official engine's dataclass so the
    SDK's ``dataclasses.asdict(variant)`` consumption works unchanged."""

    name: str
    enabled: bool
    feature_enabled: bool
    payload: Optional[Dict[str, str]] = None


class UnleashEngine:
    """Pure-evaluation engine. Holds no I/O; feed it state via ``take_state``."""

    def __init__(self) -> None:
        self._engine = NativeEngine()

    # --- state -----------------------------------------------------------
    def take_state(self, state: str) -> Optional[str]:
        """Apply a client-features JSON payload; returns a warning string or None."""
        return self._engine.take_state(state)

    def get_state(self) -> str:
        """Serialize current state back to a client-features JSON string."""
        return self._engine.get_state()

    # --- evaluation (hot path) ------------------------------------------
    def is_enabled(self, feature_name: str, context: dict) -> Optional[bool]:
        """True/False, or None when the toggle is unknown (SDK fallback signal)."""
        return self._engine.is_enabled(feature_name, context or {})

    def get_variant(self, feature_name: str, context: dict) -> Variant:
        """Resolve a variant (the 'disabled' variant when off/unknown)."""
        # yggdrasil serializes ExtendedVariantDef as camelCase JSON.
        raw = json.loads(self._engine.get_variant(feature_name, context or {}))
        return Variant(
            name=raw["name"],
            enabled=raw["enabled"],
            feature_enabled=raw["featureEnabled"],
            payload=raw.get("payload"),
        )

    # --- metrics ---------------------------------------------------------
    def count_toggle(self, feature_name: str, enabled: bool) -> None:
        self._engine.count_toggle(feature_name, enabled)

    def count_variant(self, feature_name: str, variant_name: str) -> None:
        self._engine.count_variant(feature_name, variant_name)

    def get_metrics(self) -> Optional[Dict[str, Any]]:
        raw = self._engine.get_metrics()
        return json.loads(raw) if raw is not None else None

    def should_emit_impression_event(self, feature_name: str) -> bool:
        return self._engine.should_emit_impression_event(feature_name)

    def list_known_toggles(self) -> List[Dict[str, Any]]:
        return json.loads(self._engine.list_known_toggles())

    # --- unsupported -----------------------------------------------------
    def register_custom_strategies(self, custom_strategies: dict) -> None:
        raise NotImplementedError(
            "Custom strategies are not supported by ct-unleash-engine"
        )
