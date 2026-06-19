"""Differential test: ct-unleash-engine must agree with the official
yggdrasil-engine on randomized states and contexts.

Skipped automatically when yggdrasil-engine is not installed.
"""

import dataclasses
import json
import random

import pytest

from ct_unleash_engine import UnleashEngine as Native

ygg = pytest.importorskip("yggdrasil_engine.engine")
Official = ygg.UnleashEngine

STICKINESS = ["default", "userId", "sessionId", "customField"]
OPERATORS = ["IN", "NOT_IN", "STR_CONTAINS", "STR_STARTS_WITH", "NUM_GTE", "NUM_LT"]


def _gen_constraint(rng):
    op = rng.choice(OPERATORS)
    if op in ("NUM_GTE", "NUM_LT"):
        return {
            "contextName": "score",
            "operator": op,
            "value": str(rng.randint(0, 100)),
        }
    field = rng.choice(["plan", "tier", "country"])
    vals = [rng.choice(["pro", "free", "ent", "us", "uk", "gold"]) for _ in range(2)]
    c = {"contextName": field, "operator": op, "values": vals}
    if rng.random() < 0.3:
        c["inverted"] = True
    if rng.random() < 0.3:
        c["caseInsensitive"] = True
    return c


def _gen_strategy(rng):
    name = rng.choice(
        ["default", "flexibleRollout", "userWithId", "gradualRolloutUserId"]
    )
    s = {"name": name, "constraints": [_gen_constraint(rng) for _ in range(rng.randint(0, 2))]}
    if name == "flexibleRollout":
        s["parameters"] = {
            "rollout": str(rng.choice([0, 25, 50, 100])),
            "stickiness": rng.choice(STICKINESS),
            "groupId": "g",
        }
    elif name == "userWithId":
        s["parameters"] = {"userIds": ",".join(str(rng.randint(1, 50)) for _ in range(5))}
    elif name == "gradualRolloutUserId":
        s["parameters"] = {"percentage": str(rng.choice([0, 50, 100])), "groupId": "g"}
    return s


def _gen_variants(rng):
    n = rng.randint(0, 3)
    # Real Unleash applies ONE stickiness to all of a feature's variants (the UI
    # has no per-variant stickiness). Per-variant differing stickiness with a
    # missing field is a degenerate config where engine *versions* legitimately
    # diverge, so keep the group uniform to compare like-for-like.
    stickiness = rng.choice(STICKINESS)
    return [
        {
            "name": f"v{i}",
            "weight": 1000 // max(n, 1),
            "weightType": "variable",
            "stickiness": stickiness,
            "payload": {"type": "string", "value": f"p{i}"},
        }
        for i in range(n)
    ]


def _gen_state(rng, n_features=8):
    features = []
    for i in range(n_features):
        features.append(
            {
                "name": f"feat_{i}",
                "enabled": rng.random() < 0.85,
                "type": "release",
                "project": "default",
                "strategies": [_gen_strategy(rng) for _ in range(rng.randint(0, 3))],
                "variants": _gen_variants(rng),
            }
        )
    return {"version": 2, "features": features, "segments": []}


def _gen_context(rng):
    # Always supply every field a variant/strategy might key its stickiness on
    # (userId, sessionId, customField, tier). A missing/empty stickiness field
    # makes BOTH engines fall back to an independent random bucket, which is
    # correct but non-deterministic and therefore non-comparable. Guaranteeing
    # them present keeps default/userId/sessionId/custom stickiness deterministic
    # while still exercising every stickiness mode.
    ctx = {
        "userId": str(rng.randint(1, 80)),
        "sessionId": f"s{rng.randint(1, 50)}",
    }
    props = {
        "customField": rng.choice(["a", "b", "c", "d"]),
        "tier": rng.choice(["gold", "silver", "bronze"]),
    }
    for f in ("plan", "country", "score"):
        if rng.random() < 0.6:
            props[f] = rng.choice(["pro", "free", "ent", "us", "gold", "42", "7", "x"])
    ctx["properties"] = props
    return ctx


def _norm_variant(v):
    return {"name": v.name, "enabled": v.enabled, "feature_enabled": v.feature_enabled}


def _norm_official(v):
    d = dataclasses.asdict(v)
    return {
        "name": d["name"],
        "enabled": d["enabled"],
        "feature_enabled": d["feature_enabled"],
    }


@pytest.mark.parametrize("seed", range(60))
def test_differential_parity(seed):
    rng = random.Random(seed)
    state = _gen_state(rng)
    state_json = json.dumps(state)

    native = Native()
    native.take_state(state_json)
    official = Official()
    official.take_state(state_json)

    names = [f["name"] for f in state["features"]]
    for _ in range(40):
        ctx = _gen_context(rng)
        for name in names:
            assert native.is_enabled(name, dict(ctx)) == official.is_enabled(
                name, dict(ctx)
            ), (seed, name, ctx)
            nv = _norm_variant(native.get_variant(name, dict(ctx)))
            ov = _norm_official(official.get_variant(name, dict(ctx)))
            assert nv == ov, (seed, name, ctx, nv, ov)
