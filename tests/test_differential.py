"""Differential tests: ct-unleash-engine must behave exactly like yggdrasil-engine
1.3.1, the reference engine, for every method the Unleash SDK calls.

"Exactly" means the same return values (including None vs a value, dataclass field
order and the get_state string byte for byte) and the same exception class for
the same input. Skipped automatically when yggdrasil-engine is not installed.
"""

import dataclasses
import json
import random
from datetime import datetime, timezone

import pytest

import ct_unleash_engine
from ct_unleash_engine import UnleashEngine as Native

ygg = pytest.importorskip("yggdrasil_engine.engine")
Official = ygg.UnleashEngine


def _engines(state_json):
    native, official = Native(), Official()
    native.take_state(state_json)
    official.take_state(state_json)
    return native, official


def _outcome(fn, *args):
    """("ok", value) or ("raise", exception class), comparable across engines."""
    try:
        return ("ok", _normalize(fn(*args)))
    except BaseException as exc:  # the class is part of the contract
        return ("raise", type(exc))


def _normalize(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        # Field names and their order are visible through asdict() in the SDK.
        return (type(value).__name__, list(dataclasses.asdict(value).items()))
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


# --- randomized states (evaluation semantics) -----------------------------------

STICKINESS = ["default", "userId", "sessionId", "customField"]
OPERATORS = ["IN", "NOT_IN", "STR_CONTAINS", "STR_STARTS_WITH", "NUM_GTE", "NUM_LT"]


def _gen_constraint(rng):
    op = rng.choice(OPERATORS)
    if op in ("NUM_GTE", "NUM_LT"):
        return {"contextName": "score", "operator": op, "value": str(rng.randint(0, 100))}
    field = rng.choice(["plan", "tier", "country"])
    vals = [rng.choice(["pro", "free", "ent", "us", "uk", "gold"]) for _ in range(2)]
    c = {"contextName": field, "operator": op, "values": vals}
    if rng.random() < 0.3:
        c["inverted"] = True
    if rng.random() < 0.3:
        c["caseInsensitive"] = True
    return c


def _gen_strategy(rng):
    name = rng.choice(["default", "flexibleRollout", "userWithId", "gradualRolloutUserId"])
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
    # Real Unleash applies one stickiness to all of a feature's variants; keep the
    # group uniform so missing-field fallbacks stay deterministic.
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
    features = [
        {
            "name": f"feat_{i}",
            "enabled": rng.random() < 0.85,
            "type": "release",
            "project": "default",
            "strategies": [_gen_strategy(rng) for _ in range(rng.randint(0, 3))],
            "variants": _gen_variants(rng),
            "impressionData": rng.random() < 0.3,
        }
        for i in range(n_features)
    ]
    return {"version": 2, "features": features, "segments": []}


def _gen_context(rng):
    # Every stickiness field is always present: a missing one makes both engines
    # fall back to an independent random bucket, which is correct but not comparable.
    props = {
        "customField": rng.choice(["a", "b", "c", "d"]),
        "tier": rng.choice(["gold", "silver", "bronze"]),
    }
    for f in ("plan", "country", "score"):
        if rng.random() < 0.6:
            props[f] = rng.choice(["pro", "free", "ent", "us", "gold", "42", "7", "x"])
    return {
        "userId": str(rng.randint(1, 80)),
        "sessionId": f"s{rng.randint(1, 50)}",
        "properties": props,
    }


@pytest.mark.parametrize("seed", range(60))
def test_randomized_state_parity(seed):
    rng = random.Random(seed)
    state = _gen_state(rng)
    native, official = _engines(json.dumps(state))
    names = [f["name"] for f in state["features"]] + ["unknown_toggle"]
    for _ in range(40):
        ctx = _gen_context(rng)
        for name in names:
            for method in ("is_enabled", "get_variant"):
                n = _outcome(getattr(native, method), name, dict(ctx))
                o = _outcome(getattr(official, method), name, dict(ctx))
                assert n == o, (seed, method, name, ctx, n, o)
    for name in names:
        assert native.should_emit_impression_event(name) == official.should_emit_impression_event(name)


# --- awkward contexts (conversion rules, fallbacks and errors) ------------------


def _c(ctx_name, op, values=None, value=None):
    d = {"contextName": ctx_name, "operator": op}
    if values is not None:
        d["values"] = values
    if value is not None:
        d["value"] = value
    return d


def _flag(name, constraints=None, strategy="default", params=None, variants=None):
    s = {"name": strategy, "constraints": constraints or []}
    if params:
        s["parameters"] = params
    f = {"name": name, "enabled": True, "type": "release", "project": "default", "strategies": [s]}
    if variants:
        f["variants"] = variants
    return f


# One flag per context field, so any conversion difference changes a result.
FIELD_FLAGS = [
    _flag("f_user", strategy="userWithId", params={"userIds": "1,42,7"}),
    _flag("f_userc", [_c("userId", "IN", ["42", "7", "True", "true"])]),
    _flag("f_session", [_c("sessionId", "IN", ["s1", "1"])]),
    _flag("f_env", [_c("environment", "IN", ["prod"])]),
    _flag("f_app", [_c("appName", "IN", ["bench"])]),
    _flag("f_time", [_c("currentTime", "DATE_AFTER", value="2020-01-01T00:00:00Z")]),
    _flag("f_remote", [_c("remoteAddress", "IN", ["10.0.0.1"])]),
    _flag("f_plan", [_c("plan", "IN", ["pro"])]),
    _flag("f_num", [_c("age", "NUM_GTE", value="18")]),
    _flag("f_bool", [_c("beta", "IN", ["true", "True"])]),
    _flag("f_float", [_c("score", "NUM_GT", value="1.5")]),
    _flag("f_team", [_c("team", "IN", ["payments"])]),
    _flag("f_roll", strategy="flexibleRollout", params={"rollout": "50", "stickiness": "userId", "groupId": "g"}),
    # Weighted selection falls back to a random bucket when the stickiness field is
    # missing, so only overrides decide here (zero-weight variants are never drawn).
    _flag(
        "f_variant",
        variants=[
            {"name": "a", "weight": 1000, "weightType": "variable", "stickiness": "userId", "payload": {"type": "json", "value": "{}"}},
            {"name": "b", "weight": 0, "weightType": "variable", "stickiness": "userId", "overrides": [{"contextName": "plan", "values": ["pro"]}]},
            {"name": "c", "weight": 0, "weightType": "variable", "stickiness": "userId", "payload": {"type": "string", "value": "c"}, "overrides": [{"contextName": "userId", "values": ["42"]}]},
        ],
    ),
]
FIELD_STATE = json.dumps({"version": 2, "segments": [], "features": FIELD_FLAGS})
FIELD_NAMES = [f["name"] for f in FIELD_FLAGS]
KNOWN = ["userId", "sessionId", "environment", "appName", "currentTime", "remoteAddress"]
GOOD = {
    "userId": ["42", "1", "9"],
    "sessionId": ["s1", "s2"],
    "environment": ["prod", "dev"],
    "appName": ["bench", "other"],
    "currentTime": ["2026-10-05T00:00:00Z", "2019-01-01T00:00:00Z"],
    "remoteAddress": ["10.0.0.1", "10.0.0.2"],
    "plan": ["pro", "free"],
    "age": ["30", "12"],
    "beta": ["true", "no"],
    "score": ["2.5", "1.0"],
    "team": ["payments", "growth"],
}


class _StrSub(str):
    pass


class _DictSub(dict):
    pass


def _value(rng, key):
    r = rng.random()
    if r < 0.40:
        return rng.choice(GOOD.get(key, ["x"]))
    if r < 0.52:
        return rng.choice([42, 7, 30, 12, 0, -1, 2**63, 2**64 + 3, -(2**70)])
    if r < 0.60:
        return rng.choice([True, False])
    if r < 0.68:
        return rng.choice([2.5, 1.0, 30.0, 0.1 + 0.2, 1e16, -0.0, float("nan"), float("inf")])
    if r < 0.73:
        return None
    if r < 0.79:
        return rng.choice([["a"], {"n": 1}, (), [1.5, None], {"deep": [{"x": float("nan")}]}])
    if r < 0.83:
        return _StrSub(rng.choice(GOOD.get(key, ["x"])))
    if r < 0.86:
        return rng.choice(["bad\ud800surrogate", "nul\x00inside", "ünïcödé"])
    if r < 0.88:
        return rng.choice([datetime(2026, 1, 1), object()])
    return rng.choice(GOOD.get(key, ["x"]))


def _awkward_context(rng, depth=0):
    d = {}
    for f in KNOWN:
        if rng.random() < 0.5:
            d[f] = _value(rng, f)
    r = rng.random()
    if r < 0.72:
        props = {}
        for k in ["plan", "age", "beta", "score", "team"] + KNOWN:
            if rng.random() < (0.5 if k not in KNOWN else 0.2):
                props[k] = _value(rng, k)
        if rng.random() < 0.05:
            props[rng.choice([1, 2.5, True, None])] = "odd key"
        d["properties"] = _DictSub(props) if rng.random() < 0.05 else props
    elif r < 0.80:
        d["properties"] = rng.choice(["notadict", ["l"], 5, None, 1.5])
    for k in ["team", "plan", "age", "beta"]:
        if rng.random() < 0.25:
            d[k] = _value(rng, k)
    if rng.random() < 0.03:
        d[7] = "int key"
    if depth == 0 and rng.random() < 0.08:
        d = {"context": _awkward_context(rng, 1), "userId": "ignored"} if rng.random() < 0.7 else {"context": "notadict"}
    if rng.random() < 0.04:
        d = _DictSub(d)
    return d


@pytest.mark.parametrize("seed", range(8))
def test_awkward_context_parity(seed):
    rng = random.Random(1000 + seed)
    native, official = _engines(FIELD_STATE)
    for _ in range(250):
        ctx = _awkward_context(rng)
        for name in FIELD_NAMES:
            for method in ("is_enabled", "get_variant"):
                n = _outcome(getattr(native, method), name, ctx)
                o = _outcome(getattr(official, method), name, ctx)
                assert n == o, (method, name, ctx, n, o)


def _plain_value(rng, key):
    """Only values the native fast path accepts, in every awkward combination."""
    r = rng.random()
    if r < 0.45:
        return rng.choice(GOOD.get(key, ["x"]))
    if r < 0.60:
        return rng.choice([42, 7, 30, 12, 0, -1, 2**63 - 1, -(2**63), 2**64 - 1])
    if r < 0.70:
        return rng.choice([True, False])
    if r < 0.80:
        return None
    if r < 0.90:
        return _StrSub(rng.choice(GOOD.get(key, ["x"])))
    return rng.choice(["nul\x00inside", "ünïcödé", "", " ", "42 "])


def _plain_context(rng):
    d = {}
    for f in KNOWN:
        if rng.random() < 0.45:
            d[f] = _plain_value(rng, f)
    if rng.random() < 0.85:
        props = {}
        for k in ["plan", "age", "beta", "score", "team"] + KNOWN:
            if rng.random() < (0.5 if k not in KNOWN else 0.3):
                props[k] = _plain_value(rng, k)
        d["properties"] = props
    elif rng.random() < 0.5:
        d["properties"] = _plain_value(rng, "properties")
    for k in ["team", "plan", "age", "beta", "score"]:
        if rng.random() < 0.3:
            d[k] = _plain_value(rng, k)
    return d


@pytest.mark.parametrize("seed", range(6))
def test_plain_context_fast_path_parity(seed):
    # Targets the hand-written conversion: every context here must be handled
    # natively (never by the JSON fallback) and still match 1.3.1.
    rng = random.Random(3000 + seed)
    native, official = _engines(FIELD_STATE)
    for _ in range(300):
        ctx = _plain_context(rng)
        assert native._native.is_enabled("f_plan", ctx) is not NotImplemented, ctx
        for name in FIELD_NAMES:
            for method in ("is_enabled", "get_variant"):
                n = _outcome(getattr(native, method), name, ctx)
                o = _outcome(getattr(official, method), name, ctx)
                assert n == o, (method, name, ctx, n, o)


def _sdk_safe_context(context, static=None):
    """UnleashClient 6.8.0's _safe_context: what the SDK hands the engine."""
    base = ["userId", "sessionId", "environment", "appName", "currentTime", "remoteAddress", "properties"]
    new_context = dict(static or {"appName": "bench", "environment": "prod"})
    new_context.update(context or {})
    if "currentTime" not in new_context:
        new_context["currentTime"] = datetime.now(timezone.utc).isoformat()

    def safe(value):
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    properties = new_context.get("properties", {})
    extracted = {k: v for k, v in new_context.items() if k not in base}
    extracted.update(properties)
    safe_context = {k: safe(v) for k, v in new_context.items() if k != "properties"}
    safe_context["properties"] = {k: safe(v) for k, v in extracted.items()}
    return safe_context


@pytest.mark.parametrize("seed", range(6))
def test_sdk_shaped_context_parity(seed):
    rng = random.Random(2000 + seed)
    native, official = _engines(FIELD_STATE)
    checked = 0
    while checked < 250:
        raw = _awkward_context(rng)
        try:
            ctx = _sdk_safe_context(raw)
        except Exception:
            continue  # the SDK itself rejects it before reaching an engine
        checked += 1
        for name in FIELD_NAMES:
            for method in ("is_enabled", "get_variant"):
                n = _outcome(getattr(native, method), name, ctx)
                o = _outcome(getattr(official, method), name, ctx)
                assert n == o, (method, name, raw, ctx, n, o)


CIRCULAR = {}
CIRCULAR["self"] = CIRCULAR


@pytest.mark.parametrize(
    "toggle,context",
    [
        (None, {}),
        (5, {}),
        (b"f_plan", {}),
        (["unhashable"], {}),
        ("f_plan\x00ignored", {"plan": "pro"}),
        ("bad\ud800name", {}),
        (_StrSub("f_plan"), {"plan": "pro"}),
        ("f_plan", None),
        ("f_plan", []),
        ("f_plan", [("plan", "pro")]),
        ("f_plan", "string context"),
        ("f_plan", CIRCULAR),
        ("f_plan", {"properties": CIRCULAR}),
        ("f_plan", {"plan": object()}),
        (None, {"plan": object()}),
    ],
)
def test_odd_arguments_match(toggle, context):
    native, official = _engines(FIELD_STATE)
    for method in ("is_enabled", "get_variant"):
        n = _outcome(getattr(native, method), toggle, context)
        o = _outcome(getattr(official, method), toggle, context)
        assert n == o, (method, toggle, n, o)


# --- the rest of the SDK-facing surface -------------------------------------------


def test_state_and_metadata_parity():
    rng = random.Random(7)
    payload = json.dumps(_gen_state(rng))
    native, official = _engines(payload)
    assert native.get_state() == official.get_state()
    # Core lists toggles from a hash map, so 1.3.1's own order differs between engine
    # instances; only the contents are a stable behavior.
    assert sorted(_normalize(native.list_known_toggles())) == sorted(_normalize(official.list_known_toggles()))

    # take_state returns None for valid, broken and partially parseable payloads.
    broken_strategy = json.dumps(
        {
            "version": 2,
            "features": [
                _flag("ok"),
                _flag("bad", [{"contextName": "x", "operator": "NUM_GT", "value": "NaN-ish"}]),
            ],
        }
    )
    for state in (payload, broken_strategy, '{"version": 2}', '{"features": "nope"}', "not json"):
        n_engine, o_engine = Native(), Official()
        assert _outcome(n_engine.take_state, state) == _outcome(o_engine.take_state, state), state
        assert n_engine.get_state() == o_engine.get_state(), state
    assert _outcome(Native().take_state, None) == _outcome(Official().take_state, None)


def test_metrics_parity():
    native, official = _engines(FIELD_STATE)
    calls = [
        ("count_toggle", ("f_plan", True)),
        ("count_toggle", ("f_plan", False)),
        ("count_toggle", ("f_plan", None)),
        ("count_toggle", ("f_plan", "yes")),
        ("count_toggle", ("unknown", 1)),
        ("count_toggle", ("nul\x00cut", True)),
        ("count_variant", ("f_variant", "a")),
        ("count_variant", ("f_variant", "disabled")),
    ]
    for method, args in calls:
        assert _outcome(getattr(native, method), *args) == _outcome(getattr(official, method), *args)
    n_bucket, o_bucket = native.get_metrics(), official.get_metrics()
    assert n_bucket["toggles"] == o_bucket["toggles"]
    assert set(n_bucket) == set(o_bucket)
    assert native.get_metrics() is None and official.get_metrics() is None

    class Falsy:
        def __bool__(self):
            raise ValueError("no truth value")

    for method, args in [
        ("count_toggle", (None, True)),
        ("count_toggle", ("f_plan", Falsy())),
        ("count_variant", ("f_variant", None)),
        ("should_emit_impression_event", (None,)),
    ]:
        assert _outcome(getattr(native, method), *args) == _outcome(getattr(official, method), *args), method


class _Recorder:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def apply(self, parameters, context):
        self.calls.append((dict(parameters), dict(context) if isinstance(context, dict) else context))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result(context) if callable(self.result) else self.result


@pytest.mark.parametrize(
    "result",
    [True, False, lambda ctx: ctx.get("userId") == "1", None, 1, "yes", object(), RuntimeError("boom")],
)
def test_custom_strategy_parity(result):
    state = json.dumps(
        {
            "version": 2,
            "features": [
                {
                    "name": "custom",
                    "enabled": True,
                    "strategies": [{"name": "mine", "parameters": {"p": "1"}, "constraints": []}],
                    "variants": [{"name": "v", "weight": 1000, "weightType": "variable", "stickiness": "default"}],
                },
                _flag("plain"),
            ],
        }
    )
    n_strategy, o_strategy = _Recorder(result), _Recorder(result)
    native, official = Native(), Official()
    native.register_custom_strategies({"mine": n_strategy})
    official.register_custom_strategies({"mine": o_strategy})
    native.take_state(state)
    official.take_state(state)
    for ctx in ({"userId": "1"}, {"userId": "2"}, {}, None, {"userId": object()}):
        for name in ("custom", "plain"):
            for method in ("is_enabled", "get_variant"):
                n = _outcome(getattr(native, method), name, ctx)
                o = _outcome(getattr(official, method), name, ctx)
                assert n == o, (method, name, ctx, n, o)
    # apply() runs exactly as often, with the same arguments.
    assert len(n_strategy.calls) == len(o_strategy.calls)
    assert [c[0] for c in n_strategy.calls] == [c[0] for c in o_strategy.calls]


def test_register_custom_strategy_validation_parity():
    class NoApply:
        pass

    class WrongArity:
        def apply(self, only_one):
            return True

    for strategies in ({"x": NoApply()}, {"x": WrongArity()}):
        assert _outcome(Native().register_custom_strategies, strategies) == _outcome(
            Official().register_custom_strategies, strategies
        )


def _impact_snapshot(engine):
    # Metric and sample order come from hash maps in both engines and differ between
    # instances of 1.3.1 itself, so compare them as sets.
    snapshot = []
    for metric in engine.collect_impact_metrics():
        metric = dict(metric)
        metric["samples"] = sorted(metric.get("samples", []), key=lambda s: json.dumps(s, sort_keys=True))
        snapshot.append(json.dumps(metric, sort_keys=True))
    return sorted(snapshot)


def test_impact_metrics_parity():
    native, official = Native(), Official()
    ops = [
        ("define_counter", ("requests", "Total requests")),
        ("inc_counter", ("requests",)),
        ("inc_counter", ("requests", 5, {"route": "/a"})),
        ("inc_counter", ("requests", 2**64 + 7)),
        ("inc_counter", ("requests", 1.5)),
        ("define_gauge", ("temp", "Temperature")),
        ("set_gauge", ("temp", 21.5)),
        ("set_gauge", ("temp", 3, {"room": "b"})),
        ("define_histogram", ("latency", "Latency", [0.1, 1, 10])),
        ("define_histogram", ("sizes", "Sizes")),
        ("observe_histogram", ("latency", 0.5, {"route": "/a"})),
        ("observe_histogram", ("latency", "x")),
        ("inc_counter", (None,)),
        ("define_counter", ("bad", None)),
        ("define_histogram", ("nanbucket", "h", [float("nan")])),
    ]
    for method, args in ops:
        n = _outcome(getattr(native, method), *args)
        o = _outcome(getattr(official, method), *args)
        assert n == o, (method, args, n, o)
    n_snapshot, o_snapshot = _impact_snapshot(native), _impact_snapshot(official)
    assert n_snapshot == o_snapshot
    restored = [json.loads(m) for m in n_snapshot]
    for engine in (native, official):
        engine.restore_impact_metrics(restored)
    assert _impact_snapshot(native) == _impact_snapshot(official)
    assert _outcome(native.restore_impact_metrics, [{"bad": 1}]) == _outcome(
        official.restore_impact_metrics, [{"bad": 1}]
    )


def test_reported_versions_match():
    import yggdrasil_engine

    assert ct_unleash_engine.__yggdrasil_core_version__ == yggdrasil_engine.__yggdrasil_core_version__
    assert ct_unleash_engine.YggdrasilError is ygg.YggdrasilError
