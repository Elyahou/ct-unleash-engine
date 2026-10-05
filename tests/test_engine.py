"""Unit tests for the public UnleashEngine API."""

import dataclasses
import json

import pytest

from ct_unleash_engine import FeatureDefinition, UnleashEngine, Variant


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


def test_unknown_toggle_returns_none():
    e = UnleashEngine()
    e.take_state(_state([]))
    assert e.is_enabled("nope", {}) is None


def test_default_strategy_enabled():
    e = UnleashEngine()
    e.take_state(_state([_feature("f")]))
    assert e.is_enabled("f", {}) is True


def test_disabled_feature():
    e = UnleashEngine()
    e.take_state(_state([_feature("f", enabled=False)]))
    assert e.is_enabled("f", {}) is False


def test_constraint_in_match_and_miss():
    strat = [
        {
            "name": "default",
            "constraints": [
                {"contextName": "plan", "operator": "IN", "values": ["pro"]}
            ],
        }
    ]
    e = UnleashEngine()
    e.take_state(_state([_feature("f", strategies=strat)]))
    assert e.is_enabled("f", {"properties": {"plan": "pro"}}) is True
    assert e.is_enabled("f", {"properties": {"plan": "free"}}) is False


def test_flexible_rollout_100_with_user():
    strat = [
        {
            "name": "flexibleRollout",
            "parameters": {"rollout": "100", "stickiness": "default", "groupId": "f"},
        }
    ]
    e = UnleashEngine()
    e.take_state(_state([_feature("f", strategies=strat)]))
    assert e.is_enabled("f", {"userId": "1"}) is True


def test_get_variant_none_for_unknown():
    # 1.3.1 returns None when the engine has no variant; the SDK then substitutes
    # its own DISABLED_VARIATION and logs.
    e = UnleashEngine()
    e.take_state(_state([]))
    assert e.get_variant("nope", {}) is None


def test_variant_fields_match_reference_order():
    # The SDK returns asdict(variant) to callers, so field order is visible.
    assert [f.name for f in dataclasses.fields(Variant)] == [
        "name",
        "payload",
        "enabled",
        "feature_enabled",
    ]


def test_get_variant_resolves_and_is_asdict_able():
    variants = [
        {"name": "a", "weight": 1000, "weightType": "variable", "stickiness": "default"}
    ]
    e = UnleashEngine()
    e.take_state(_state([_feature("f", variants=variants)]))
    v = e.get_variant("f", {"userId": "1"})
    assert v.name == "a"
    assert v.feature_enabled is True
    # The SDK consumes the variant via dataclasses.asdict — must not raise.
    d = dataclasses.asdict(v)
    assert d["name"] == "a" and d["feature_enabled"] is True


def test_metrics_counting_and_bucket_shape():
    e = UnleashEngine()
    e.take_state(_state([_feature("f")]))
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
    # Like 1.3.1: the native parse failure is silent, then the custom-strategy
    # parser's json.loads raises JSONDecodeError (a ValueError).
    e = UnleashEngine()
    with pytest.raises(json.JSONDecodeError):
        e.take_state("not json at all")


def test_take_state_returns_none_even_with_warnings():
    # 1.3.1 never returns warnings, so the SDK never logs them.
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
    assert e.is_enabled("g", {}) is True


def test_take_state_get_state_roundtrip_preserves_eval():
    strat = [
        {
            "name": "default",
            "constraints": [
                {"contextName": "appName", "operator": "IN", "values": ["A"]}
            ],
        }
    ]
    e = UnleashEngine()
    e.take_state(_state([_feature("f", strategies=strat)]))
    snapshot = e.get_state()
    e2 = UnleashEngine()
    e2.take_state(snapshot)
    for ctx in ({"appName": "A"}, {"appName": "B"}):
        assert e2.is_enabled("f", ctx) == e.is_enabled("f", ctx)


def test_non_dict_properties_does_not_crash():
    # A raw/bootstrapped context whose "properties" is not a dict must be
    # tolerated (skipped), not crash the evaluation.
    e = UnleashEngine()
    e.take_state(_state([_feature("f")]))
    assert e.is_enabled("f", {"properties": ["not", "a", "dict"]}) is True


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
    assert e.is_enabled("f", {"userId": "1"}) is True
    assert e.is_enabled("f", {"userId": "2"}) is False
    assert custom.calls[0] == ({"list": "a"}, {"userId": "1"})


def test_custom_strategy_without_implementation_is_false():
    strat = [{"name": "unregistered", "parameters": {}, "constraints": []}]
    e = UnleashEngine()
    e.take_state(_state([_feature("f", strategies=strat)]))
    assert e.is_enabled("f", {}) is False


def test_list_known_toggles_returns_feature_definitions():
    e = UnleashEngine()
    e.take_state(_state([_feature("f")]))
    toggles = e.list_known_toggles()
    assert toggles == [FeatureDefinition(name="f", project="default", type="release")]


def test_count_toggle_uses_truthiness_like_ctypes():
    # The SDK passes whatever a fallback function returned.
    e = UnleashEngine()
    e.take_state(_state([_feature("f")]))
    for value in (None, "", 0, [], "yes", 1, [0]):
        e.count_toggle("f", value)
    bucket = e.get_metrics()
    assert bucket["toggles"]["f"] == {"yes": 3, "no": 4, "variants": {}}


def test_should_emit_impression_event():
    e = UnleashEngine()
    e.take_state(_state([_feature("f", impressionData=True), _feature("g")]))
    assert e.should_emit_impression_event("f") is True
    assert e.should_emit_impression_event("g") is False
    assert e.should_emit_impression_event("unknown") is False
