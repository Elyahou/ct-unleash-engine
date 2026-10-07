"""End to end: UnleashClient 6.9.0 must behave the same on ct-unleash-engine as on
yggdrasil-engine 2.0.0, the engine it ships with. Skipped when the SDK is missing.

The client creates its engine from the name ``UnleashEngine`` in
``UnleashClient.clients.unleash_client``; patching that name is the whole swap.
"""

import json
import random
from datetime import datetime, timezone

import pytest

client_module = pytest.importorskip("UnleashClient.clients.unleash_client")
pytest.importorskip("yggdrasil_engine.engine")

from UnleashClient import UnleashClient  # noqa: E402
from UnleashClient.cache import FileCache  # noqa: E402
from UnleashClient.impact_metrics import MetricFlagContext  # noqa: E402
from UnleashClient.utils import InstanceAllowType  # noqa: E402
from yggdrasil_engine.engine import UnleashEngine as Official  # noqa: E402

from ct_unleash_engine import UnleashEngine as Native  # noqa: E402


def _flag(name, constraints=(), strategy="default", params=None, **extra):
    s = {"name": strategy, "constraints": list(constraints)}
    if params:
        s["parameters"] = params
    f = {"name": name, "enabled": True, "type": "release", "project": "default", "strategies": [s]}
    f.update(extra)
    return f


FLAGS = [
    _flag("f_plan", [{"contextName": "plan", "operator": "IN", "values": ["pro"]}], impressionData=True),
    _flag("f_off", enabled=False),
    _flag("f_age", [{"contextName": "age", "operator": "NUM_GTE", "value": "18"}]),
    _flag("f_semver", [{"contextName": "version", "operator": "SEMVER_EQ", "value": "3.0.0-beta+build2"}]),
    _flag("f_roll", strategy="flexibleRollout", params={"rollout": "50", "stickiness": "userId", "groupId": "g"}),
    _flag(
        "f_variant",
        variants=[
            {"name": "a", "weight": 500, "weightType": "variable", "stickiness": "userId", "payload": {"type": "string", "value": "A"}},
            {"name": "b", "weight": 500, "weightType": "variable", "stickiness": "userId", "overrides": [{"contextName": "plan", "values": ["pro"]}]},
        ],
        impressionData=True,
    ),
]
STATE = {"version": 2, "segments": [], "features": FLAGS}
NAMES = [f["name"] for f in FLAGS] + ["unknown"]


def _client(engine_class, directory, monkeypatch, events=None):
    monkeypatch.setattr(client_module, "UnleashEngine", engine_class)
    cache = FileCache("parity", directory=str(directory))
    cache.bootstrap_from_dict(STATE)
    client = UnleashClient(
        url="http://127.0.0.1:9/api",
        app_name="parity",
        environment="prod",
        disable_metrics=True,
        disable_registration=True,
        cache=cache,
        refresh_interval=3600,
        multiple_instance_mode=InstanceAllowType.SILENTLY_ALLOW,
        event_callback=events.append if events is not None else None,
    )
    client.initialize_client(fetch_toggles=False)
    assert type(client._engine) is engine_class
    return client


@pytest.fixture
def clients(tmp_path, monkeypatch):
    native = _client(Native, tmp_path / "native", monkeypatch)
    official = _client(Official, tmp_path / "official", monkeypatch)
    yield native, official
    native.destroy()
    official.destroy()


def _raw_context(rng):
    """What an application passes to the client; the SDK enriches and stringifies it.
    currentTime is fixed so both clients see the same context, and userId is always
    there: without the stickiness field, both engines draw a random variant."""
    ctx = {"currentTime": "2026-10-07T12:00:00+00:00"}
    ctx["userId"] = rng.choice(["1", "42", 7, None, 2.5, True])
    if rng.random() < 0.5:
        ctx["sessionId"] = f"s{rng.randint(1, 9)}"
    if rng.random() < 0.8:
        ctx["properties"] = {
            "plan": rng.choice(["pro", "free", None, 1]),
            "version": rng.choice(["3.0.0-beta+build1", "3.0.0"]),
            "age": rng.choice([30, "12", 18.0, None]),
        }
    if rng.random() < 0.3:
        ctx[rng.choice(["plan", "team"])] = rng.choice(["pro", datetime(2026, 1, 1, tzinfo=timezone.utc)])
    return ctx


def _toggles(client):
    bucket = client._engine.get_metrics()
    return None if bucket is None else bucket["toggles"]


def test_flag_checks_match(clients):
    native, official = clients
    rng = random.Random(42)
    fallbacks = [None, lambda name, context: True, lambda name, context: 1 / 0]
    for _ in range(300):
        ctx = _raw_context(rng)
        for name in NAMES:
            assert native.is_enabled(name, ctx) == official.is_enabled(name, ctx), (name, ctx)
            fallback = rng.choice(fallbacks)
            assert native.is_enabled(name, ctx, fallback_function=fallback) == official.is_enabled(
                name, ctx, fallback_function=fallback
            ), (name, ctx)
            assert native.get_variant(name, ctx) == official.get_variant(name, ctx), (name, ctx)
    assert native.feature_definitions() == official.feature_definitions()
    assert _toggles(native) == _toggles(official)


def _impact(client):
    # Sample order comes from a hash map, so compare the collection as a set.
    snapshot = []
    for metric in client.impact_metrics.collect() or []:
        metric = dict(metric)
        metric["samples"] = sorted(metric.get("samples", []), key=lambda s: json.dumps(s, sort_keys=True))
        snapshot.append(json.dumps(metric, sort_keys=True))
    return sorted(snapshot)


def test_impact_metrics_match(clients):
    for client in clients:
        metrics = client.impact_metrics
        metrics.define_counter("checks", "Flag checks")
        metrics.increment_counter("checks")
        # Labels come from check_variant on the caller's raw context, including one
        # the engine cannot serialise.
        for context in ({"userId": "42", "plan": "pro"}, {"userId": "1"}, {"userId": object()}):
            metrics.increment_counter(
                "checks", 2, MetricFlagContext(flag_names=["f_variant", "f_plan", "unknown"], context=context)
            )
        metrics.define_gauge("temp", "Temperature")
        metrics.update_gauge("temp", 21.5, MetricFlagContext(flag_names=["f_off"], context={}))
        metrics.define_histogram("latency", "Latency", [0.1, 1])
        metrics.observe_histogram("latency", 0.5, MetricFlagContext(flag_names=["f_plan"], context={"plan": "pro"}))
    native, official = clients
    assert _impact(native) == _impact(official)


def _event(event):
    return (
        event.event_type,
        getattr(event, "feature_name", None),
        getattr(event, "enabled", None),
        getattr(event, "variant", None),
        getattr(event, "context", None),
    )


def test_impression_events_match(tmp_path, monkeypatch):
    received = {"native": [], "official": []}
    clients = {
        "native": _client(Native, tmp_path / "native", monkeypatch, received["native"]),
        "official": _client(Official, tmp_path / "official", monkeypatch, received["official"]),
    }
    ctx = {"userId": "42", "currentTime": "2026-10-07T12:00:00+00:00", "properties": {"plan": "pro"}}
    for client in clients.values():
        for name in NAMES:
            client.is_enabled(name, ctx)
            client.get_variant(name, ctx)
        client.destroy()  # drains the event queue
    native, official = ([_event(e) for e in received[k]] for k in ("native", "official"))
    assert native == official
    assert sum(1 for e in native if e[1] is not None) == 4  # f_plan and f_variant, twice each
