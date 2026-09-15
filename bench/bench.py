"""Benchmark ct-unleash-engine (PyO3) against the installed yggdrasil-engine (ctypes).

Usage:
    pip install yggdrasil-engine          # 1.3.x from PyPI, or a local build of main
    maturin develop --release
    python bench/bench.py

Works with both the 1.x API (`is_enabled` -> Optional[bool]) and the 2.0 API
(`check_enabled` -> FeatureToggle). Rows are aligned so both engines do the same
work: "eval only" is one evaluation; "eval + impression + count" adds the
`should_emit_impression_event` and `count_toggle` calls that 2.0's `is_enabled`
performs internally.
"""

import json
import os
import statistics
from importlib.metadata import PackageNotFoundError, distribution
import sys
import threading
import time

import yggdrasil_engine
from yggdrasil_engine.engine import UnleashEngine as Official

from ct_unleash_engine import UnleashEngine as PyO3

STATE = json.dumps(
    {
        "version": 2,
        "segments": [],
        "features": [
            {
                "name": "simple",
                "enabled": True,
                "type": "release",
                "project": "default",
                "strategies": [{"name": "default"}],
            },
            {
                "name": "rollout",
                "enabled": True,
                "type": "release",
                "project": "default",
                "strategies": [
                    {
                        "name": "flexibleRollout",
                        "parameters": {
                            "rollout": "50",
                            "stickiness": "default",
                            "groupId": "rollout",
                        },
                        "constraints": [
                            {
                                "contextName": "plan",
                                "operator": "IN",
                                "values": ["pro", "ent"],
                            }
                        ],
                    }
                ],
                "variants": [
                    {
                        "name": "a",
                        "weight": 500,
                        "weightType": "variable",
                        "stickiness": "default",
                        "payload": {"type": "string", "value": "A"},
                    },
                    {
                        "name": "b",
                        "weight": 500,
                        "weightType": "variable",
                        "stickiness": "default",
                    },
                ],
            },
        ],
    }
)

CONTEXT = {
    "userId": "12345",
    "sessionId": "sess-1",
    "appName": "bench",
    "environment": "prod",
    "properties": {"plan": "pro", "tier": "gold", "country": "us", "region": "eu-west-1"},
}

N = int(os.environ.get("BENCH_N", 200_000))
THREADS = int(os.environ.get("BENCH_THREADS", 8))
PER_THREAD = int(os.environ.get("BENCH_PER_THREAD", 20_000))


def ns_per_call(fn, n=N):
    fn()
    fn()
    start = time.perf_counter_ns()
    for _ in range(n):
        fn()
    return (time.perf_counter_ns() - start) / n


def threaded(fn, threads=THREADS, per=PER_THREAD):
    lats = [[] for _ in range(threads)]

    def worker(i):
        for _ in range(per):
            t = time.perf_counter_ns()
            fn()
            lats[i].append(time.perf_counter_ns() - t)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    start = time.perf_counter_ns()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    wall = time.perf_counter_ns() - start
    flat = sorted(x for lat in lats for x in lat)
    return {
        "p50": flat[len(flat) // 2],
        "p99": flat[int(len(flat) * 0.99)],
        "ops/s": (threads * per) / (wall / 1e9),
    }


def make_official_ops(engine):
    """Return (eval_only, eval_full, variant_only) callables for either API."""
    if hasattr(engine, "check_enabled"):  # 2.0 API
        return (
            lambda: engine.check_enabled("rollout", CONTEXT),
            lambda: engine.is_enabled("rollout", CONTEXT),
            lambda: engine.check_variant("rollout", CONTEXT),
        )

    def full():  # 1.x: replicate what 2.0's is_enabled does
        r = engine.is_enabled("rollout", CONTEXT)
        engine.should_emit_impression_event("rollout")
        engine.count_toggle("rollout", bool(r))
        return r

    return (
        lambda: engine.is_enabled("rollout", CONTEXT),
        full,
        lambda: engine.get_variant("rollout", CONTEXT),
    )


def make_pyo3_ops(engine):
    def full():
        r = engine.is_enabled("rollout", CONTEXT)
        engine.should_emit_impression_event("rollout")
        engine.count_toggle("rollout", bool(r))
        return r

    return (
        lambda: engine.is_enabled("rollout", CONTEXT),
        full,
        lambda: engine.get_variant("rollout", CONTEXT),
    )


def main():
    official = Official()
    official.take_state(STATE)
    pyo3 = PyO3()
    pyo3.take_state(STATE)

    # Only trust the dist metadata if the imported module actually came from
    # that installation; a checkout on PYTHONPATH shadows the installed wheel.
    upstream_version = "(local checkout)"
    try:
        dist = distribution("yggdrasil-engine")
        installed_dir = os.path.join(str(dist.locate_file("")), "yggdrasil_engine")
        if os.path.dirname(os.path.abspath(yggdrasil_engine.__file__)) == os.path.abspath(installed_dir):
            upstream_version = dist.version
    except PackageNotFoundError:
        pass
    api = "2.0" if hasattr(official, "check_enabled") else "1.x"
    print(f"python {sys.version.split()[0]}")
    print(f"yggdrasil-engine {upstream_version} ({api} API) from {os.path.dirname(yggdrasil_engine.__file__)}")
    print(f"ct-unleash-engine from {os.path.dirname(sys.modules['ct_unleash_engine'].__file__)}")
    if hasattr(os, "getloadavg"):
        print(f"load avg {os.getloadavg()}")

    labels = ("eval only", "eval + impression + count", "variant, eval only")
    off_ops = make_official_ops(official)
    py_ops = make_pyo3_ops(pyo3)

    print(f"\n{'call (single thread)':30s} {'ctypes':>10s} {'PyO3':>10s} {'ratio':>7s}")
    for label, o, p in zip(labels, off_ops, py_ops):
        # two rounds each, report the median so a busy machine skews less
        a = statistics.median(ns_per_call(o) for _ in range(2))
        b = statistics.median(ns_per_call(p) for _ in range(2))
        print(f"{label:30s} {a:8.0f} ns {b:8.0f} ns {a / b:6.1f}x")

    print(f"\n{THREADS} threads, eval + impression + count:")
    for name, fn in (("ctypes", off_ops[1]), ("PyO3", py_ops[1])):
        r = threaded(fn)
        print(f"  {name:7s} p50 {r['p50']:9,.0f} ns   p99 {r['p99']:11,.0f} ns   {r['ops/s']:10,.0f} ops/s")


if __name__ == "__main__":
    main()
