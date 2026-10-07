"""Workload and timing helpers shared by the benchmark scripts.

Free of engine and SDK imports, so each script runs in an environment that holds
only the engine or SDK it measures.
"""

import json
import os
import threading
import time

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

# What the engine benchmarks pass: a context as the SDK hands it to the engine.
CONTEXT = {
    "userId": "12345",
    "sessionId": "sess-1",
    "appName": "bench",
    "environment": "prod",
    "properties": {"plan": "pro", "tier": "gold", "country": "us", "region": "eu-west-1"},
}

# What the SDK benchmarks pass, as an application would; the SDK adds appName,
# environment and currentTime.
SDK_CONTEXT = {
    "userId": "12345",
    "sessionId": "sess-1",
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
