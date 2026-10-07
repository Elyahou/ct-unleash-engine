"""Engine baseline for yggdrasil-engine 1.x, the engine of UnleashClient 6.8 and older.

Usage, in an environment with the 1.x engine:
    pip install "yggdrasil-engine==1.3.1"
    python bench/bench_1x.py

1.x has no check_* methods and does not count inside is_enabled, so each row times
the 1.x calls that do the same work as the 2.0 method it is named after. The rows
line up with the ctypes column of bench.py.
"""

import statistics
import sys
from importlib.metadata import version

from yggdrasil_engine.engine import UnleashEngine

from _common import CONTEXT, STATE, THREADS, ns_per_call, threaded


def main():
    engine = UnleashEngine()
    if hasattr(engine, "check_enabled"):
        sys.exit("This baseline needs yggdrasil-engine 1.x; use bench/bench.py for 2.0 and later.")
    engine.take_state(STATE)

    def check_enabled():
        enabled = engine.is_enabled("rollout", CONTEXT)
        engine.should_emit_impression_event("rollout")
        return enabled

    def is_enabled():
        engine.count_toggle("rollout", bool(check_enabled()))

    def check_variant():
        variant = engine.get_variant("rollout", CONTEXT)
        engine.should_emit_impression_event("rollout")
        return variant

    def get_variant():
        variant = check_variant()
        engine.count_toggle("rollout", variant.feature_enabled if variant else False)
        engine.count_variant("rollout", variant.name if variant else "disabled")

    print(f"python {sys.version.split()[0]}  yggdrasil-engine {version('yggdrasil-engine')}")
    print(f"\n{'same work as (2.0)':20s} {'ctypes':>10s}")
    for label, fn in (
        ("is_enabled", is_enabled),
        ("check_enabled", check_enabled),
        ("get_variant", get_variant),
        ("check_variant", check_variant),
    ):
        print(f"{label:20s} {statistics.median(ns_per_call(fn) for _ in range(2)):8.0f} ns")

    r = threaded(is_enabled)
    print(f"\n{THREADS} threads, is_enabled:")
    print(f"  ctypes  p50 {r['p50']:9,.0f} ns   p99 {r['p99']:11,.0f} ns   {r['ops/s']:10,.0f} ops/s")


if __name__ == "__main__":
    main()
