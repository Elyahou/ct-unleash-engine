# ct-unleash-engine

A fast [PyO3](https://pyo3.rs) binding to [`yggdrasil-core`](https://crates.io/crates/unleash-yggdrasil),
the Rust evaluation engine behind Unleash, that **behaves exactly like
`yggdrasil-engine` 2.0.0**, the engine `UnleashClient` 6.9 uses.

Same methods, same results, same return types, and the same exceptions and log
records for the same input. Only faster. For each flag check, the official engine
`json.dumps`-es the context and crosses `ctypes` three times: to evaluate, to look up
impression data, and to count. This binding reads the context directly from the
Python `dict` and does all three in one native call. It holds the GIL for that
sub-microsecond call, so there is no GIL release and re-acquire, and no p99 tail
under thread contention.

For `UnleashClient` 6.8.1 and older, which use the 1.3.1 engine API, use the 0.2.x
line instead (branch `fix/exact-1.3.1-parity`).

| Python 3.13, ns per call | yggdrasil-engine 2.0.0 | ct-unleash-engine |
|---|---:|---:|
| `is_enabled` | 11,637 | 905 |
| `check_enabled` | 10,956 | 882 |
| `get_variant` | 13,740 | 1,773 |
| `check_variant` | 12,073 | 1,568 |
| 8 threads, `is_enabled` p99 | 1,298,667 | 1,208 |
| 8 threads, `is_enabled` ops/s | 27,276 | 938,856 |

Through `UnleashClient` 6.9.0, on its own engine and on this one:

| Python 3.13, ns per call | stock | ct-unleash-engine |
|---|---:|---:|
| `client.is_enabled` | 14,994 | 3,259 |
| `client.get_variant` | 19,048 | 5,718 |
| 8 threads, `client.is_enabled` p99 | 1,720,625 | 4,333 |
| 8 threads, `client.is_enabled` ops/s | 24,923 | 285,094 |

The numbers are medians of three runs of `bench/bench.py` and `bench/sdk_bench.py`
on an Apple M-series laptop under heavy background load. Through the SDK, about
2,000 ns of each call is the client building its context, which no engine can
remove. On Python 3.8 and 3.9 the engine calls take roughly 1,400 to 2,100 ns,
because the 3.8 wheel has to copy each string (see *Wheels* below).

## How it stays identical

- **Same core.** `unleash-yggdrasil` 0.21.5, `unleash-types` 0.16.2, `serde_json`
  1.0.151 and the other crates that decide evaluation, such as `pest` 2.9.0 and
  `rand` 0.9.5, are pinned to what 2.0.0's native library (0.20.8) was built with.
- **Plain contexts take the fast path.** An exact `dict` with `str` keys whose
  values are `str`, `None`, `bool` or a 64-bit `int`, with `properties` of the same
  shape. That is what the SDK always passes. These values survive 2.0.0's
  `json.dumps` and serde round trip unchanged, so the binding applies
  unleash-types' context rules to them directly: known fields are promoted out of
  `properties`, other top-level strings are merged into it, and numbers and bools
  are stringified.
- **Everything else replays 2.0.0's steps.** Floats, nested containers, dict or key
  subclasses, non-`str` keys, a nested `context` key, strings that aren't valid
  UTF-8, and odd toggle names all go through `json.dumps` and the same serde
  parsing as 2.0.0's native library. Results and exceptions for those inputs match
  by construction.
- **Same steps, in the same order.** Evaluate, ask the fallback function about an
  unknown toggle, look up impression data, then count. A failed step stops the
  ones after it, so metric counts match even when an evaluation fails.
- **Never raises, like 2.0.0.** On any `Exception`, the four evaluation methods
  return the disabled answer and log the same warning, with the same exception
  attached, on the same `yggdrasil_engine.engine` logger. A `BaseException` that
  is not an `Exception` passes through, as it does in 2.0.0.
- **Same result classes.** `FeatureToggle`, `FeatureVariant`, `Variant` and
  `FeatureDefinition` are verbatim copies of 2.0.0's. They print, compare and
  serialise identically, and `FeatureToggle` refuses `bool()` with the same error.
- **Toggles with custom strategies** always take the exact path, because 2.0.0
  serialises the context before calling a strategy's `apply()`. They run about 3x
  faster than on 2.0.0, against more than 10x for other toggles.
- **The SDK-visible details match too.** `take_state` always returns `None`,
  `get_state()` matches byte for byte, and `count_toggle` truthiness and the
  C-string handling of names match as well.
- **Same exception class.** When `yggdrasil-engine` is installed, which the SDK
  guarantees, errors are raised as `yggdrasil_engine.engine.YggdrasilError` itself.

The only orderings that differ are ones that are random in 2.0.0 itself: the order
of `list_known_toggles()` and of impact-metric samples changes between engine
instances there too.

## API

The full surface of `yggdrasil_engine.engine.UnleashEngine` 2.0.0:

```python
from ct_unleash_engine import UnleashEngine

engine = UnleashEngine()
engine.take_state(client_features_json)   # full or delta payload; returns None
engine.is_enabled("my_flag", context)     # FeatureToggle(name, is_enabled, is_found,
                                          #   requires_impression_event_emission); counted
engine.is_enabled("my_flag", context, fallback_function=fn)  # fn(name, context) answers unknown toggles
engine.check_enabled("my_flag", context)  # same answer, not counted
engine.get_variant("my_flag", context)    # FeatureVariant(name, variant, is_found,
                                          #   requires_impression_event_emission); counted
engine.check_variant("my_flag", context)  # same answer, not counted
engine.register_custom_strategies({"name": strategy_with_apply})
engine.count_toggle("my_flag", True)
engine.count_variant("my_flag", "variant_name")
engine.should_emit_impression_event("my_flag")
engine.get_metrics()                      # bucket dict for /client/metrics, or None
engine.get_state()                        # client-features JSON (the SDK caches it)
engine.list_known_toggles()               # [FeatureDefinition(name, project, type)]
# impact metrics
engine.define_counter(name, help_text); engine.inc_counter(name, value=1, labels=None)
engine.define_gauge(name, help_text); engine.set_gauge(name, value, labels=None)
engine.define_histogram(name, help_text, buckets=None); engine.observe_histogram(name, value, labels=None)
engine.collect_impact_metrics(); engine.restore_impact_metrics(metrics)
```

## Building and testing locally

```bash
pip install maturin
maturin develop --release                       # builds + installs into the active venv
pip install "yggdrasil-engine==2.0.0" "UnleashClient==6.9.0" pytest
CT_CLIENT_SPEC_PATH=/path/to/client-specification/specifications pytest tests/
```

The suite runs four layers:

- **Unit tests** for each method.
- **The Unleash client specification**, version 6.1.0.
- **Differential tests against yggdrasil-engine 2.0.0.** They compare results,
  exception classes, log records and metric counts over randomized flag states,
  adversarial contexts, contexts shaped by the SDK, odd arguments, fallback
  functions, custom strategies, metrics and impact metrics.
- **End-to-end tests with `UnleashClient` 6.9.0.** The client runs on each engine
  in turn, and flag checks, variants, feature definitions, impact metrics and
  impression events must match.

## Benchmark

`bench/bench.py` compares this binding against the installed `yggdrasil-engine`
2.x, method by method. `bench/sdk_bench.py` runs `UnleashClient` 6.9.0 on each
engine and times the calls an application makes.

```bash
pip install "UnleashClient==6.9.0"
maturin develop --release
python bench/bench.py
python bench/sdk_bench.py
```

`bench/bench_1x.py` and `bench/sdk_bench_68.py` time the same rows on the previous
generation, `yggdrasil-engine` 1.3.1 and `UnleashClient` 6.8. Run them in a separate
environment:

```bash
pip install "UnleashClient==6.8.1"   # brings yggdrasil-engine 1.3.1
python bench/bench_1x.py
python bench/sdk_bench_68.py
```

## Wheels

CI (`.github/workflows/wheels.yml`) builds two abi3 wheels per platform, for
`manylinux` x86_64 and aarch64 and for macOS:

- **`cp38-abi3`** runs on CPython 3.8+. The 3.8 stable ABI can't expose a
  string's UTF-8 buffer, so each context string is copied.
- **`cp310-abi3`** runs on CPython 3.10+ and borrows the strings, which is
  noticeably faster.

pip installs the newest wheel an interpreter supports, so 3.8 stays supported
while 3.10+ gets the faster build. Your deploy target's platform must match the
wheel: Linux pods need a `manylinux` wheel, not a local macOS build.

```bash
maturin build --release                                              # cp38-abi3
maturin build --release --no-default-features --features abi3-py310  # cp310-abi3
```

Install in an application:

```
# requirements.txt (from your private index, or a pinned git tag)
ct-unleash-engine==0.3.0
```

## Using it as the SDK engine

`UnleashClient` 6.9 creates its engine from the name `UnleashEngine` in
`UnleashClient.clients.unleash_client`, and `AsyncUnleashClient` from the same name
in `UnleashClient.clients.async_unleash_client`. Point that name at this package
before creating a client:

```python
import UnleashClient.clients.unleash_client as unleash_client
from ct_unleash_engine import UnleashEngine

unleash_client.UnleashEngine = UnleashEngine
# For AsyncUnleashClient, which needs the SDK's "async" extra:
# import UnleashClient.clients.async_unleash_client as async_unleash_client
# async_unleash_client.UnleashEngine = UnleashEngine
```

These module paths are SDK internals, so pin `UnleashClient` to the version the
tests cover. The SDK keeps reporting `yggdrasil_engine.__yggdrasil_core_version__`
to the Unleash server from the installed `yggdrasil-engine`, so registration and
metrics payloads don't change.
