"""Benchmark ct-unleash-engine (PyO3) against the installed yggdrasil-engine (ctypes).

Usage:
    pip install "yggdrasil-engine>=2.0"
    maturin develop --release
    python bench/bench.py

Both engines expose the 2.0 API, so each row times the same method on each. What
the methods do in 2.0: `check_enabled` evaluates and looks up whether an
impression event is due; `is_enabled` also counts the answer towards
`get_metrics()`. `check_variant` and `get_variant` do the same for variants, and
`get_variant` counts both the toggle and the variant. 2.0 has no public call that
only evaluates. `bench_1x.py` times yggdrasil-engine 1.x on the same rows.
"""

import os
import statistics
import sys
from importlib.metadata import PackageNotFoundError, distribution

import yggdrasil_engine
from yggdrasil_engine.engine import UnleashEngine as Official

from ct_unleash_engine import UnleashEngine as PyO3

from _common import CONTEXT, STATE, THREADS, ns_per_call, threaded


def ops(engine):
    return {
        "is_enabled": lambda: engine.is_enabled("rollout", CONTEXT),
        "check_enabled": lambda: engine.check_enabled("rollout", CONTEXT),
        "get_variant": lambda: engine.get_variant("rollout", CONTEXT),
        "check_variant": lambda: engine.check_variant("rollout", CONTEXT),
    }


def main():
    official = Official()
    if not hasattr(official, "check_enabled"):
        sys.exit("This benchmark needs yggdrasil-engine 2.0 or later; use bench/bench_1x.py for 1.x.")
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
    print(f"python {sys.version.split()[0]}")
    print(f"yggdrasil-engine {upstream_version} from {os.path.dirname(yggdrasil_engine.__file__)}")
    print(f"ct-unleash-engine from {os.path.dirname(sys.modules['ct_unleash_engine'].__file__)}")
    if hasattr(os, "getloadavg"):
        print(f"load avg {os.getloadavg()}")

    off_ops, py_ops = ops(official), ops(pyo3)
    print(f"\n{'call (single thread)':20s} {'ctypes':>10s} {'PyO3':>10s} {'ratio':>7s}")
    for label in off_ops:
        # two rounds each, report the median so a busy machine skews less
        a = statistics.median(ns_per_call(off_ops[label]) for _ in range(2))
        b = statistics.median(ns_per_call(py_ops[label]) for _ in range(2))
        print(f"{label:20s} {a:8.0f} ns {b:8.0f} ns {a / b:6.1f}x")

    print(f"\n{THREADS} threads, is_enabled:")
    for name, fn in (("ctypes", off_ops["is_enabled"]), ("PyO3", py_ops["is_enabled"])):
        r = threaded(fn)
        print(f"  {name:7s} p50 {r['p50']:9,.0f} ns   p99 {r['p99']:11,.0f} ns   {r['ops/s']:10,.0f} ops/s")


if __name__ == "__main__":
    main()
