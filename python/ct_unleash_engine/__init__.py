"""Drop-in replacement for yggdrasil-engine 1.3.1's ``UnleashEngine``: same results,
return types and exceptions, backed by a PyO3 binding to the same yggdrasil core.

Plain contexts (an exact ``dict`` of ``str`` keys whose values are str, None, bool
or 64-bit ints, which is what the Unleash SDK always passes) are read directly in
Rust with no JSON. Anything else replays 1.3.1's exact steps: ``json.dumps`` here,
then the native ``*_json`` methods, which parse with the same serde types and
versions as 1.3.1's native library. That fallback is what keeps odd inputs
identical, so it must not be "simplified" into the fast path.
"""

from __future__ import annotations

import ctypes
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ._native import NativeEngine, __core_version__, _set_error_class
from .custom_strategy import CustomStrategyHandler

try:
    # Raise the very class the reference engine raises, so code that catches
    # ``yggdrasil_engine.engine.YggdrasilError`` keeps working. The Unleash SDK
    # depends on yggdrasil-engine, so it is normally installed.
    from yggdrasil_engine.engine import YggdrasilError
except ImportError:  # pragma: no cover - only without yggdrasil-engine installed

    class YggdrasilError(Exception):  # type: ignore[no-redef]
        pass


_set_error_class(YggdrasilError)

# The value yggdrasil-engine 1.3.1 exposes (its bundled native library version),
# which the SDK reports to the Unleash server as "yggdrasilVersion". Kept identical
# so registration and metrics payloads don't change when swapping engines.
__yggdrasil_core_version__ = "0.20.7"

__all__ = [
    "FeatureDefinition",
    "UnleashEngine",
    "Variant",
    "YggdrasilError",
    "__core_version__",
    "__yggdrasil_core_version__",
    "load_feature_defs",
]


@dataclass
class Variant:
    """Same fields, order and defaults as 1.3.1: the SDK returns
    ``asdict(variant)`` to callers, so field order shows up in their dicts."""

    name: str
    payload: Optional[Dict[str, str]] = field(default_factory=dict)
    enabled: bool = False
    feature_enabled: bool = False

    @staticmethod
    def from_dict(data: dict) -> "Variant":
        return Variant(
            name=data.get("name", ""),
            payload=data.get("payload"),
            enabled=data.get("enabled", False),
            feature_enabled=data.get("featureEnabled", False),
        )


@dataclass
class FeatureDefinition:
    name: str
    project: str
    type: Optional[str]

    @staticmethod
    def from_dict(data: dict) -> "FeatureDefinition":
        return FeatureDefinition(
            name=data.get("name", ""),
            project=data.get("project", ""),
            type=data.get("type"),
        )


def load_feature_defs(raw_defs: List[dict]) -> List[FeatureDefinition]:
    return [FeatureDefinition.from_dict(defn) for defn in raw_defs]


def _c_bytes(value: Any) -> Optional[bytes]:
    """What 1.3.1 hands ctypes for a string argument: ``value.encode("utf-8")``,
    raising whatever that raises, then accepted by ``c_char_p`` only as bytes or
    None (None becomes a NULL pointer, which the native side reports as an error)."""
    raw = value.encode("utf-8")
    if raw is None or isinstance(raw, bytes):
        return raw
    raise ctypes.ArgumentError("wrong type")


def _c_number(ctype: Any, value: Any, position: int) -> Any:
    """ctypes converts a numeric argument with the type's own setter (which masks
    out-of-range ints and rejects the wrong kind) and reports a failure as
    ``ctypes.ArgumentError``."""
    try:
        return ctype(value).value
    except Exception as cause:
        raise ctypes.ArgumentError(
            f"argument {position}: {type(cause).__name__}: {cause}"
        ) from None


def _variant(raw: Any) -> Optional[Variant]:
    if raw is None:
        return None
    name, payload_type, payload_value, enabled, feature_enabled = raw
    # 1.3.1 builds this with Variant.from_dict on the native JSON, which omits
    # "payload" when there is none.
    payload = (
        None
        if payload_type is None
        else {"type": payload_type, "value": payload_value}
    )
    return Variant(
        name=name, payload=payload, enabled=enabled, feature_enabled=feature_enabled
    )


class UnleashEngine:
    """Pure-evaluation engine. Holds no I/O; feed it state via ``take_state``."""

    def __init__(self) -> None:
        self._native = NativeEngine()
        self.custom_strategy_handler = CustomStrategyHandler()
        # Bound once: these run on every flag check.
        self._fast_is_enabled = self._native.is_enabled
        self._fast_get_variant = self._native.get_variant
        self._fast_count_toggle = self._native.count_toggle

    # --- state -------------------------------------------------------------

    def take_state(self, state_json: str) -> None:
        self._native.take_state(_c_bytes(state_json))
        self.custom_strategy_handler.update_strategies(state_json)
        # 1.3.1 returns None in every case: its native call reports warnings as an
        # error status and the Python side never reads it. Returning warnings here
        # would make the SDK log a warning the reference engine never triggers.
        return None

    def get_state(self) -> str:
        raw = self._native.get_state()
        # 1.3.1 re-serialises the native JSON with json.dumps; the SDK writes this
        # string to its cache, so the formatting has to match too.
        return json.dumps(json.loads(raw) if raw is not None else None)

    # --- evaluation (hot path) ----------------------------------------------

    def is_enabled(self, toggle_name: str, context: dict) -> Optional[bool]:
        ctx = context or {}
        definitions = self.custom_strategy_handler.strategy_definitions
        if definitions and type(toggle_name) is str and toggle_name in definitions:
            return self._is_enabled_exact(toggle_name, context, ctx)
        result = self._fast_is_enabled(toggle_name, ctx)
        if result is NotImplemented:
            return self._is_enabled_exact(toggle_name, context, ctx)
        return result

    def get_variant(self, toggle_name: str, context: dict) -> Optional[Variant]:
        ctx = context or {}
        definitions = self.custom_strategy_handler.strategy_definitions
        if definitions and type(toggle_name) is str and toggle_name in definitions:
            return _variant(self._get_variant_exact(toggle_name, context, ctx))
        raw = self._fast_get_variant(toggle_name, ctx)
        if raw is NotImplemented:
            raw = self._get_variant_exact(toggle_name, context, ctx)
        return _variant(raw)

    # 1.3.1's own steps, in its order. Toggles with custom strategies always come
    # here: 1.3.1 serialises the context before calling a strategy's apply(), which
    # may mutate it, and only then evaluates.

    def _is_enabled_exact(self, toggle_name: Any, context: Any, ctx: Any) -> Optional[bool]:
        serialized_context = json.dumps(ctx)
        custom_results = json.dumps(
            self.custom_strategy_handler.evaluate_custom_strategies(toggle_name, context)
        )
        return self._native.is_enabled_json(
            _c_bytes(toggle_name),
            serialized_context.encode("utf-8"),
            custom_results.encode("utf-8"),
        )

    def _get_variant_exact(self, toggle_name: Any, context: Any, ctx: Any) -> Any:
        serialized_context = json.dumps(ctx)
        custom_results = json.dumps(
            self.custom_strategy_handler.evaluate_custom_strategies(toggle_name, context)
        )
        return self._native.get_variant_json(
            _c_bytes(toggle_name),
            serialized_context.encode("utf-8"),
            custom_results.encode("utf-8"),
        )

    def register_custom_strategies(self, custom_strategies: dict) -> None:
        self.custom_strategy_handler.register_custom_strategies(custom_strategies)

    # --- metrics -------------------------------------------------------------

    def count_toggle(self, toggle_name: str, enabled: bool) -> None:
        if self._fast_count_toggle(toggle_name, enabled) is NotImplemented:
            self._native.count_toggle_bytes(_c_bytes(toggle_name), enabled)

    def count_variant(self, toggle_name: str, variant_name: str) -> None:
        if self._native.count_variant(toggle_name, variant_name) is NotImplemented:
            self._native.count_variant_bytes(
                _c_bytes(toggle_name), _c_bytes(variant_name)
            )

    def get_metrics(self) -> Optional[Dict[str, Any]]:
        raw = self._native.get_metrics()
        return json.loads(raw) if raw is not None else None

    def should_emit_impression_event(self, toggle_name: str) -> bool:
        result = self._native.should_emit_impression_event(toggle_name)
        if result is NotImplemented:
            result = self._native.should_emit_impression_event_bytes(
                _c_bytes(toggle_name)
            )
        return result

    def list_known_toggles(self) -> List[FeatureDefinition]:
        return load_feature_defs(json.loads(self._native.list_known_toggles()))

    # --- impact metrics (argument handling mirrors 1.3.1 line by line) --------

    def define_counter(self, name: str, help_text: str) -> None:
        self._native.define_counter(_c_bytes(name), _c_bytes(help_text))

    def inc_counter(
        self, name: str, value: int = 1, labels: Optional[Dict[str, str]] = None
    ) -> None:
        labels_json = json.dumps(labels).encode("utf-8") if labels else None
        name_bytes = _c_bytes(name)
        self._native.inc_counter(
            name_bytes, _c_number(ctypes.c_int64, value, 3), labels_json
        )

    def collect_impact_metrics(self) -> List[Dict[str, Any]]:
        return json.loads(self._native.collect_impact_metrics()) or []

    def restore_impact_metrics(self, metrics: List[Dict[str, Any]]) -> None:
        metrics_json = json.dumps(metrics).encode("utf-8")
        self._native.restore_impact_metrics(metrics_json)

    def define_gauge(self, name: str, help_text: str) -> None:
        self._native.define_gauge(_c_bytes(name), _c_bytes(help_text))

    def set_gauge(
        self, name: str, value: float, labels: Optional[Dict[str, str]] = None
    ) -> None:
        labels_json = json.dumps(labels).encode("utf-8") if labels else None
        name_bytes = _c_bytes(name)
        self._native.set_gauge(
            name_bytes, _c_number(ctypes.c_double, value, 3), labels_json
        )

    def define_histogram(
        self, name: str, help_text: str, buckets: Optional[List[float]] = None
    ) -> None:
        buckets_json = json.dumps(buckets if buckets is not None else []).encode(
            "utf-8"
        )
        self._native.define_histogram(
            _c_bytes(name), _c_bytes(help_text), buckets_json
        )

    def observe_histogram(
        self, name: str, value: float, labels: Optional[Dict[str, str]] = None
    ) -> None:
        labels_json = json.dumps(labels).encode("utf-8") if labels else None
        name_bytes = _c_bytes(name)
        self._native.observe_histogram(
            name_bytes, _c_number(ctypes.c_double, value, 3), labels_json
        )
