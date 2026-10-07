"""Benchmark UnleashClient 6.9.0 end to end on its own engine and on ct-unleash-engine.

Usage:
    pip install "UnleashClient==6.9.0"      # brings yggdrasil-engine 2.0
    maturin develop --release
    python bench/sdk_bench.py

Times the calls an application makes (`client.is_enabled`, `client.get_variant`),
so the numbers include the SDK's own work: building the context, emitting events,
and so on. "context enrichment" times that building step alone, which is the floor
no engine can go below. `sdk_bench_68.py` times UnleashClient 6.8 on the same rows.
"""

import json
import os
import statistics
import sys
import tempfile
from importlib.metadata import version

import UnleashClient.clients.unleash_client as client_module
from UnleashClient import UnleashClient
from UnleashClient.cache import FileCache
from UnleashClient.utils import InstanceAllowType
from yggdrasil_engine.engine import UnleashEngine as Official

from ct_unleash_engine import UnleashEngine as PyO3

from _common import N, PER_THREAD, SDK_CONTEXT, STATE, THREADS, ns_per_call, threaded


def make_client(engine_class):
    # The client builds its engine from this module-level name.
    client_module.UnleashEngine = engine_class
    cache = FileCache("bench", directory=tempfile.mkdtemp())
    cache.bootstrap_from_dict(json.loads(STATE))
    client = UnleashClient(
        url="http://127.0.0.1:9/api",
        app_name="bench",
        environment="prod",
        disable_metrics=True,
        disable_registration=True,
        cache=cache,
        refresh_interval=3600,
        multiple_instance_mode=InstanceAllowType.SILENTLY_ALLOW,
    )
    client.initialize_client(fetch_toggles=False)
    assert type(client._engine) is engine_class
    return client


def ops(client):
    return {
        "context enrichment": lambda: client._evaluator._enricher.build(SDK_CONTEXT),
        "client.is_enabled": lambda: client.is_enabled("rollout", SDK_CONTEXT),
        "client.get_variant": lambda: client.get_variant("rollout", SDK_CONTEXT),
    }


def main():
    official, pyo3 = make_client(Official), make_client(PyO3)
    print(f"python {sys.version.split()[0]}  UnleashClient {version('UnleashClient')}  "
          f"yggdrasil-engine {version('yggdrasil-engine')}  ct-unleash-engine {version('ct-unleash-engine')}")
    if hasattr(os, "getloadavg"):
        print(f"load avg {os.getloadavg()}")
    print(f"{N:,} calls per row, {THREADS} threads x {PER_THREAD:,} calls")

    off_ops, py_ops = ops(official), ops(pyo3)
    print(f"\n{'call (single thread)':22s} {'stock':>10s} {'PyO3':>10s} {'ratio':>7s}")
    for label in off_ops:
        a = statistics.median(ns_per_call(off_ops[label]) for _ in range(2))
        b = statistics.median(ns_per_call(py_ops[label]) for _ in range(2))
        print(f"{label:22s} {a:8.0f} ns {b:8.0f} ns {a / b:6.1f}x")

    print(f"\n{THREADS} threads, client.is_enabled:")
    for name, fn in (("stock", off_ops["client.is_enabled"]), ("PyO3", py_ops["client.is_enabled"])):
        r = threaded(fn)
        print(f"  {name:6s} p50 {r['p50']:9,.0f} ns   p99 {r['p99']:11,.0f} ns   {r['ops/s']:10,.0f} ops/s")

    official.destroy()
    pyo3.destroy()


if __name__ == "__main__":
    main()
