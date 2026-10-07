"""Unit tests for the public UnleashEngine API (yggdrasil-engine 2.0.0 semantics)."""

import dataclasses
import json
import logging

import pytest

import ct_unleash_engine
from ct_unleash_engine import (
    FeatureDefinition,
    FeatureToggle,
    FeatureVariant,
    UnleashEngine,
    Variant,
    disabled_variant,
)


def _state(features, segments=None):
    return json.dumps({"version": 2, "features": features, "segments": segments or []})


def _feature(name, enabled=True, strategies=None, variants=None, **extra):
    f = {
        "name": name,
        "enabled": enabled,
        "type": "release",
        "project": "default",
        "strategies": strategies if strategies is not None else [{"name": "default"}],
    }
    if variants is not None:
        f["variants"] = variants
    f.update(extra)
    return f


def _engine(*features):
    e = UnleashEngine()
    e.take_state(_state(list(features)))
    return e


def _toggles(engine):
    bucket = engine.get_metrics()
    return bucket["toggles"] if bucket else {}


def test_unknown_toggle_is_disabled_and_not_found():
    result = _engine().is_enabled("nope", {})
    assert result == FeatureToggle(name="nope", is_enabled=False, is_found=False)


def test_default_strategy_enabled():
    result = _engine(_feature("f")).is_enabled("f", {})
    assert result.is_enabled is True and result.is_found is True


def test_disabled_feature_is_found():
    result = _engine(_feature("f", enabled=False)).is_enabled("f", {})
    assert result.is_enabled is False and result.is_found is True


def test_constraint_in_match_and_miss():
    strat = [
        {
            "name": "default",
            "constraints": [
                {"contextName": "plan", "operator": "IN", "values": ["pro"]}
            ],
        }
    ]
    e = _engine(_feature("f", strategies=strat))
    assert e.is_enabled("f", {"properties": {"plan": "pro"}}).is_enabled is True
    assert e.is_enabled("f", {"properties": {"plan": "free"}}).is_enabled is False


def test_flexible_rollout_100_with_user():
    strat = [
        {
            "name": "flexibleRollout",
            "parameters": {"rollout": "100", "stickiness": "default", "groupId": "f"},
        }
    ]
    assert _engine(_feature("f", strategies=strat)).is_enabled("f", {"userId": "1"}).is_enabled


def test_feature_toggle_has_no_truth_value():
    # 2.0.0 makes `if engine.is_enabled(...)` an error, so 1.x-style callers fail
    # loudly instead of always seeing a truthy object.
    with pytest.raises(TypeError, match="has no truth value"):
        bool(_engine(_feature("f")).is_enabled("f", {}))


def test_fallback_only_for_unknown_toggles():
    calls = []

    def fallback(name, context):
        calls.append((name, context))
        return True

    e = _engine(_feature("f", enabled=False))
    assert e.is_enabled("f", {}, fallback_function=fallback).is_enabled is False
    unknown = e.is_enabled("nope", {"userId": "1"}, fallback_function=fallback)
    assert unknown.is_enabled is True and unknown.is_found is False
    assert calls == [("nope", {"userId": "1"})]


def test_failing_fallback_gives_disabled_toggle_and_a_warning(caplog):
    def fallback(name, context):
        raise RuntimeError("boom")

    e = _engine()
    with caplog.at_level(logging.WARNING, logger="yggdrasil_engine.engine"):
        result = e.is_enabled("nope", {}, fallback_function=fallback)
    assert result == FeatureToggle(name="nope")
    [record] = caplog.records
    assert record.name == "yggdrasil_engine.engine"
    assert record.getMessage().startswith("Failed to evaluate toggle nope, returning FeatureToggle(")
    assert record.exc_info[0] is RuntimeError
    # Nothing is counted when the evaluation fails.
    assert _toggles(e) == {}


def test_is_enabled_counts_and_check_enabled_does_not():
    e = _engine(_feature("f"))
    e.check_enabled("f", {})
    assert _toggles(e) == {}
    e.is_enabled("f", {})
    e.is_enabled("nope", {})
    toggles = _toggles(e)
    assert toggles["f"]["yes"] == 1
    assert toggles["nope"]["no"] == 1


def test_get_variant_for_unknown_toggle_is_the_disabled_variant():
    result = _engine().get_variant("nope", {})
    assert result == FeatureVariant(
        name="nope",
        variant=disabled_variant(),
        is_found=False,
        requires_impression_event_emission=False,
    )


def test_variant_fields_match_reference_order():
    # The SDK returns asdict(result.variant) to callers, so field order is visible.
    assert [f.name for f in dataclasses.fields(Variant)] == [
        "name",
        "payload",
        "enabled",
        "feature_enabled",
    ]


def test_get_variant_resolves_and_counts():
    variants = [
        {
            "name": "a",
            "weight": 1000,
            "weightType": "variable",
            "stickiness": "default",
            "payload": {"type": "string", "value": "x"},
        }
    ]
    e = _engine(_feature("f", variants=variants))
    checked = e.check_variant("f", {"userId": "1"})
    assert _toggles(e) == {}
    result = e.get_variant("f", {"userId": "1"})
    assert result == checked
    assert result.is_found is True
    assert result.variant == Variant(
        name="a", payload={"type": "string", "value": "x"}, enabled=True, feature_enabled=True
    )
    toggles = _toggles(e)
    assert toggles["f"]["yes"] == 1 and toggles["f"]["variants"] == {"a": 1}


def test_feature_variant_is_frozen():
    result = _engine().get_variant("nope", {})
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.is_found = True


def test_impression_data_is_reported_on_results():
    e = _engine(_feature("f", impressionData=True), _feature("g"))
    assert e.is_enabled("f", {}).requires_impression_event_emission is True
    assert e.check_variant("f", {}).requires_impression_event_emission is True
    assert e.is_enabled("g", {}).requires_impression_event_emission is False
    assert e.should_emit_impression_event("f") is True
    assert e.should_emit_impression_event("unknown") is False


def test_metrics_counting_and_bucket_shape():
    e = _engine(_feature("f"))
    assert e.get_metrics() is None  # nothing counted yet
    e.count_toggle("f", True)
    e.count_toggle("f", True)
    e.count_toggle("f", False)
    bucket = e.get_metrics()
    assert set(bucket) >= {"start", "stop", "toggles"}
    assert bucket["toggles"]["f"]["yes"] == 2
    assert bucket["toggles"]["f"]["no"] == 1
    # bucket reset after read
    assert e.get_metrics() is None


def test_take_state_invalid_json_raises():
    # Like 2.0.0: the native parse failure is silent, then the custom-strategy
    # parser's json.loads raises JSONDecodeError (a ValueError).
    with pytest.raises(json.JSONDecodeError):
        UnleashEngine().take_state("not json at all")


def test_take_state_returns_none_even_with_warnings():
    # 2.0.0 never returns warnings, so the SDK never logs them.
    broken = _feature(
        "f",
        strategies=[
            {
                "name": "default",
                "constraints": [
                    {"contextName": "x", "operator": "NUM_GT", "value": "not-a-number"}
                ],
            }
        ],
    )
    e = UnleashEngine()
    assert e.take_state(_state([broken, _feature("g")])) is None
    assert e.is_enabled("g", {}).is_enabled is True


def test_take_state_get_state_roundtrip_preserves_eval():
    strat = [
        {
            "name": "default",
            "constraints": [
                {"contextName": "appName", "operator": "IN", "values": ["A"]}
            ],
        }
    ]
    e = _engine(_feature("f", strategies=strat))
    e2 = UnleashEngine()
    e2.take_state(e.get_state())
    for ctx in ({"appName": "A"}, {"appName": "B"}):
        assert e2.check_enabled("f", ctx) == e.check_enabled("f", ctx)


def test_non_dict_properties_does_not_crash():
    # A raw context whose "properties" is not a dict is tolerated (skipped).
    assert _engine(_feature("f")).is_enabled("f", {"properties": ["not", "a", "dict"]}).is_enabled


class _Allow:
    def __init__(self, allowed):
        self.allowed = allowed
        self.calls = []

    def apply(self, parameters, context):
        self.calls.append((parameters, context))
        return context.get("userId") in self.allowed


def test_custom_strategy_is_evaluated():
    strat = [{"name": "allowList", "parameters": {"list": "a"}, "constraints": []}]
    e = UnleashEngine()
    custom = _Allow({"1"})
    e.register_custom_strategies({"allowList": custom})
    e.take_state(_state([_feature("f", strategies=strat)]))
    assert e.is_enabled("f", {"userId": "1"}).is_enabled is True
    assert e.is_enabled("f", {"userId": "2"}).is_enabled is False
    assert custom.calls[0] == ({"list": "a"}, {"userId": "1"})


def test_custom_strategy_without_implementation_is_false():
    strat = [{"name": "unregistered", "parameters": {}, "constraints": []}]
    assert _engine(_feature("f", strategies=strat)).is_enabled("f", {}).is_enabled is False


def test_list_known_toggles_returns_feature_definitions():
    toggles = _engine(_feature("f")).list_known_toggles()
    assert toggles == [FeatureDefinition(name="f", project="default", type="release")]


def test_count_toggle_uses_truthiness_like_ctypes():
    e = _engine(_feature("f"))
    for value in (None, "", 0, [], "yes", 1, [0]):
        e.count_toggle("f", value)
    assert _toggles(e)["f"] == {"yes": 3, "no": 4, "variants": {}}


def test_semver_ignores_build_metadata_like_core_0_21_5():
    # Core 0.21.5 compares semver by precedence, ignoring build metadata; a build
    # against an older core fails here.
    strat = [
        {
            "name": "default",
            "constraints": [
                {"contextName": "version", "operator": "SEMVER_EQ", "value": "3.0.0-beta+build2"}
            ],
        }
    ]
    e = _engine(_feature("f", strategies=strat))
    assert e.is_enabled("f", {"properties": {"version": "3.0.0-beta+build1"}}).is_enabled is True


def test_versions():
    assert ct_unleash_engine.__core_version__ == "0.21.5"
    assert ct_unleash_engine.__yggdrasil_core_version__ == "0.20.8"
