"""Drop-in replacement for yggdrasil-engine 2.0.0's ``UnleashEngine``: same results,
return types, exceptions and log records, backed by a PyO3 binding to the same
yggdrasil core.

Plain contexts (an exact ``dict`` of ``str`` keys whose values are str, None, bool
or 64-bit ints, which is what the Unleash SDK always passes) are read directly in
Rust with no JSON, and one native call evaluates, looks up impression data and
counts. Anything else replays 2.0.0's exact steps: ``json.dumps`` here, then the
native ``*_json`` methods, which parse with the same serde types and versions as
2.0.0's native library. That fallback is what keeps odd inputs identical, so it
must not be "simplified" into the fast path.
"""

from __future__ import annotations

import ctypes
import json
import logging
from typing import Any, Callable, Dict, List, Optional

from ._models import (
    FeatureDefinition,
    FeatureToggle,
    FeatureVariant,
    Variant,
    disabled_variant,
    load_feature_defs,
)
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

# 2.0.0 logs evaluation failures on its own module logger. Logging under the same
# name keeps existing logging configuration (levels, handlers, filters) in effect
# when swapping engines.
_logger = logging.getLogger("yggdrasil_engine.engine")

# The value yggdrasil-engine 2.0.0 exposes (its bundled native library version),
# which the SDK reports to the Unleash server as "yggdrasilVersion".
__yggdrasil_core_version__ = "0.20.8"

__all__ = [
    "FeatureDefinition",
    "FeatureToggle",
    "FeatureVariant",
    "UnleashEngine",
    "Variant",
    "YggdrasilError",
    "__core_version__",
    "__yggdrasil_core_version__",
    "disabled_variant",
    "load_feature_defs",
]


def _c_bytes(value: Any) -> Optional[bytes]:
    """What 2.0.0 hands ctypes for a string argument: ``value.encode("utf-8")``,
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


def _to_variant(raw: Any) -> Optional[Variant]:
    if raw is None:
        return None
    name, payload_type, payload_value, enabled, feature_enabled = raw
    # 2.0.0 builds this with Variant.from_dict on the native JSON, which omits
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
        self._resolve_toggle = self._native.resolve_toggle
        self._resolve_variant = self._native.resolve_variant

    # --- state -------------------------------------------------------------

    def take_state(self, state_json: str) -> None:
        self._native.take_state(_c_bytes(state_json))
        self.custom_strategy_handler.update_strategies(state_json)
        # 2.0.0 returns the native result's value, which is always empty (see the
        # native take_state), so it returns None in every case. Returning warnings
        # here would make the SDK log warnings the reference engine never reports.
        return None

    def get_state(self) -> str:
        raw = self._native.get_state()
        # 2.0.0 re-serialises the native JSON with json.dumps; the SDK writes this
        # string to its cache, so the formatting has to match too.
        return json.dumps(json.loads(raw) if raw is not None else None)

    # --- evaluation (hot path) ----------------------------------------------
    #
    # Like 2.0.0, these never raise: any failure yields the disabled answer and a
    # warning, with the exception attached, on the ``yggdrasil_engine.engine`` logger.

    def is_enabled(
        self,
        toggle_name: str,
        context: dict,
        *,
        fallback_function: Optional[Callable[[str, dict], Any]] = None,
    ) -> FeatureToggle:
        """Evaluate a toggle and count the answer towards ``get_metrics()``."""
        try:
            return self._evaluate_toggle(toggle_name, context, fallback_function, True)
        except Exception:
            result = FeatureToggle(name=toggle_name)
            _logger.warning(
                "Failed to evaluate toggle %s, returning %s",
                toggle_name,
                result,
                exc_info=True,
            )
            return result

    def check_enabled(
        self,
        toggle_name: str,
        context: dict,
        *,
        fallback_function: Optional[Callable[[str, dict], Any]] = None,
    ) -> FeatureToggle:
        """Evaluate a toggle without counting it."""
        try:
            return self._evaluate_toggle(toggle_name, context, fallback_function, False)
        except Exception:
            result = FeatureToggle(name=toggle_name)
            _logger.warning(
                "Failed to evaluate toggle %s, returning %s",
                toggle_name,
                result,
                exc_info=True,
            )
            return result

    def get_variant(self, toggle_name: str, context: dict) -> FeatureVariant:
        """Resolve a toggle's variant and count the toggle and the variant."""
        try:
            return self._evaluate_variant(toggle_name, context, True)
        except Exception:
            result = FeatureVariant(
                name=toggle_name,
                variant=disabled_variant(),
                is_found=False,
                requires_impression_event_emission=False,
            )
            _logger.warning(
                "Failed to evaluate variant for toggle %s, returning %s",
                toggle_name,
                result,
                exc_info=True,
            )
            return result

    def check_variant(self, toggle_name: str, context: dict) -> FeatureVariant:
        """Resolve a toggle's variant without counting anything."""
        try:
            return self._evaluate_variant(toggle_name, context, False)
        except Exception:
            result = FeatureVariant(
                name=toggle_name,
                variant=disabled_variant(),
                is_found=False,
                requires_impression_event_emission=False,
            )
            _logger.warning(
                "Failed to evaluate variant for toggle %s, returning %s",
                toggle_name,
                result,
                exc_info=True,
            )
            return result

    # 2.0.0's steps, in its order: evaluate (``context or {}`` first), consult the
    # fallback for an unknown toggle, look up impression data, then count. The
    # native fast path does all of it in one call; everything it declines takes
    # 2.0.0's own route below. Toggles with custom strategies always take that route,
    # because 2.0.0 serialises the context before calling a strategy's apply(),
    # which may mutate it, and only then evaluates.

    def _evaluate_toggle(
        self,
        toggle_name: Any,
        context: Any,
        fallback_function: Optional[Callable[[str, dict], Any]],
        count: bool,
    ) -> FeatureToggle:
        ctx = context or {}
        definitions = self.custom_strategy_handler.strategy_definitions
        if not (definitions and type(toggle_name) is str and toggle_name in definitions):
            resolved = self._resolve_toggle(
                toggle_name, ctx, count, fallback_function is not None
            )
            if resolved is None:
                # Unknown toggle: the fallback answers before anything is looked up
                # or counted.
                value = fallback_function(toggle_name, context)  # type: ignore[misc]
                return self._finish_toggle(toggle_name, value, False, count)
            if resolved is not NotImplemented:
                is_enabled, is_found, impression = resolved
                return FeatureToggle(
                    name=toggle_name,
                    is_enabled=is_enabled,
                    is_found=is_found,
                    requires_impression_event_emission=impression,
                )
        value = self._is_enabled_exact(toggle_name, context, ctx)
        is_found = value is not None
        if value is None and fallback_function is not None:
            value = fallback_function(toggle_name, context)
        return self._finish_toggle(toggle_name, value, is_found, count)

    def _finish_toggle(
        self, toggle_name: Any, value: Any, is_found: bool, count: bool
    ) -> FeatureToggle:
        is_enabled = bool(value)
        result = FeatureToggle(
            name=toggle_name,
            is_enabled=is_enabled,
            is_found=is_found,
            requires_impression_event_emission=bool(
                self.should_emit_impression_event(toggle_name)
            ),
        )
        if count:
            self.count_toggle(toggle_name, is_enabled)
        return result

    def _evaluate_variant(
        self, toggle_name: Any, context: Any, count: bool
    ) -> FeatureVariant:
        ctx = context or {}
        definitions = self.custom_strategy_handler.strategy_definitions
        if not (definitions and type(toggle_name) is str and toggle_name in definitions):
            resolved = self._resolve_variant(toggle_name, ctx, count)
            if resolved is not NotImplemented:
                raw, impression = resolved
                value = _to_variant(raw)
                return FeatureVariant(
                    name=toggle_name,
                    variant=value if value is not None else disabled_variant(),
                    is_found=value is not None,
                    requires_impression_event_emission=impression,
                )
        value = _to_variant(self._get_variant_exact(toggle_name, context, ctx))
        variant = value if value is not None else disabled_variant()
        result = FeatureVariant(
            name=toggle_name,
            variant=variant,
            is_found=value is not None,
            requires_impression_event_emission=bool(
                self.should_emit_impression_event(toggle_name)
            ),
        )
        if count:
            self.count_toggle(toggle_name, variant.feature_enabled)
            self.count_variant(toggle_name, variant.name)
        return result

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
        if self._native.count_toggle(toggle_name, enabled) is NotImplemented:
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

    # --- impact metrics (argument handling mirrors 2.0.0 line by line) --------

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
