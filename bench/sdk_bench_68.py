"""Client baseline for UnleashClient 6.8 and older, on its own engine (yggdrasil-engine 1.x).

Usage, in an environment with the 6.8 SDK:
    pip install "UnleashClient==6.8.1"      # brings yggdrasil-engine 1.3.1
    python bench/sdk_bench_68.py

Same flags, context and timing as sdk_bench.py, so the rows line up with its stock
column for 6.9.
"""

import json
import statistics
import sys
import tempfile
from importlib.metadata import version

from UnleashClient import UnleashClient
from UnleashClient.cache import FileCache

from _common import N, PER_THREAD, SDK_CONTEXT, STATE, THREADS, ns_per_call, threaded


def main():
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
    )
    # 6.9 moved context building into _ContextEnricher; this script measures the
    # client that still builds it in _safe_context.
    if not hasattr(client, "_safe_context"):
        sys.exit("This baseline needs UnleashClient 6.8 or older; use bench/sdk_bench.py for 6.9.")
    client.initialize_client(fetch_toggles=False)

    print(f"python {sys.version.split()[0]}  UnleashClient {version('UnleashClient')}  "
          f"yggdrasil-engine {version('yggdrasil-engine')}")
    print(f"{N:,} calls per row, {THREADS} threads x {PER_THREAD:,} calls")
    print(f"\n{'call (single thread)':22s} {'stock':>10s}")
    for label, fn in (
        ("context enrichment", lambda: client._safe_context(SDK_CONTEXT)),
        ("client.is_enabled", lambda: client.is_enabled("rollout", SDK_CONTEXT)),
        ("client.get_variant", lambda: client.get_variant("rollout", SDK_CONTEXT)),
    ):
        print(f"{label:22s} {statistics.median(ns_per_call(fn) for _ in range(2)):8.0f} ns")

    r = threaded(lambda: client.is_enabled("rollout", SDK_CONTEXT))
    print(f"\n{THREADS} threads, client.is_enabled:")
    print(f"  stock  p50 {r['p50']:9,.0f} ns   p99 {r['p99']:11,.0f} ns   {r['ops/s']:10,.0f} ops/s")
    client.destroy()


if __name__ == "__main__":
    main()
