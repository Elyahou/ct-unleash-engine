"""Unit tests for the public UnleashEngine API."""

import dataclasses
import json

import pytest

from ct_unleash_engine import UnleashEngine, Variant


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


def test_get_variant_disabled_for_unknown():
    e = UnleashEngine()
    e.take_state(_state([]))
    v = e.get_variant("nope", {})
    assert isinstance(v, Variant)
    assert v.name == "disabled"
    assert v.enabled is False
    assert v.feature_enabled is False


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
    e = UnleashEngine()
    with pytest.raises(ValueError):
        e.take_state("not json at all")


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


def test_custom_strategies_unsupported():
    e = UnleashEngine()
    with pytest.raises(NotImplementedError):
        e.register_custom_strategies({"x": lambda *_: True})


def test_should_emit_impression_event():
    e = UnleashEngine()
    e.take_state(_state([_feature("f", impressionData=True), _feature("g")]))
    assert e.should_emit_impression_event("f") is True
    assert e.should_emit_impression_event("g") is False
    assert e.should_emit_impression_event("unknown") is False
