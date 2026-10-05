# ct-unleash-engine

A fast [PyO3](https://pyo3.rs) binding to [`yggdrasil-core`](https://crates.io/crates/unleash-yggdrasil),
the Rust evaluation engine behind Unleash, that **behaves exactly like
`yggdrasil-engine` 1.3.1**, the engine `UnleashClient` 6.x uses.

Same methods, same results, same return types, and the same exceptions for the
same input. Only faster: the official engine `json.dumps`-es the context and
crosses `ctypes` on every call, while this binding reads the context directly
from the Python `dict`. It holds the GIL for the whole sub-microsecond call, so
there is no GIL release and re-acquire, and no p99 tail under thread contention.

| Python 3.13, ns per call | yggdrasil-engine 1.3.1 | ct-unleash-engine |
|---|---:|---:|
| `is_enabled` | ~7,000 | ~560 |
| `is_enabled` + impression check + `count_toggle` | ~11,100 | ~750 |
| `get_variant` | ~7,900 | ~1,030 |
| 8 threads, p99 of the second row | ~1,413,000 | ~1,080 |

On Python 3.8 and 3.9 the same calls take roughly 920, 1,230 and 1,420 ns,
because the 3.8 wheel has to copy each string (see *Wheels* below). Numbers come
from `bench/bench.py` on an Apple M-series laptop.

## How it stays identical

- **Same core.** `unleash-yggdrasil` 0.21.3, `unleash-types` 0.16.1 and
  `serde_json` 1.0.150 are pinned to exactly what 1.3.1's native library was
  built with (its binary reports core 0.21.3).
- **Plain contexts take the fast path.** An exact `dict` with `str` keys whose
  values are `str`, `None`, `bool` or a 64-bit `int`, with `properties` of the same
  shape. That is what the SDK always passes. These values survive 1.3.1's
  `json.dumps` and serde round trip unchanged, so the binding applies
  unleash-types' context rules to them directly: known fields are promoted out of
  `properties`, other top-level strings are merged into it, and numbers and bools
  are stringified.
- **Everything else replays 1.3.1's steps.** Floats, nested containers, dict or key
  subclasses, non-`str` keys, a nested `context` key, strings that aren't valid
  UTF-8, and odd toggle names all go through `json.dumps` and the same serde
  parsing as 1.3.1's native library. Results and exceptions for those inputs match
  by construction.
- **Toggles with custom strategies** always take that exact path, because 1.3.1
  serialises the context before calling a strategy's `apply()`. They evaluate
  correctly but are not faster than 1.3.1.
- **The SDK-visible details match too.** These include `take_state` always
  returning `None`, `get_variant` returning `None` when there is no variant,
  `Variant` and `FeatureDefinition` dataclasses with the same fields and order, and
  `get_state()` byte for byte. Truthiness and C-string handling of `count_toggle`
  arguments match as well.
- **Same exception class.** When `yggdrasil-engine` is installed, which the SDK
  guarantees, errors are raised as `yggdrasil_engine.engine.YggdrasilError` itself.

The only orderings that differ are ones that are random in 1.3.1 itself: the order
of `list_known_toggles()` and of impact-metric samples changes between engine
instances there too.

## API

The full surface of `yggdrasil_engine.engine.UnleashEngine` 1.3.1 that the SDK calls:

```python
from ct_unleash_engine import UnleashEngine

engine = UnleashEngine()
engine.take_state(client_features_json)       # full or delta payload; returns None
engine.is_enabled("my_flag", context)          # True / False, or None for an unknown toggle
engine.get_variant("my_flag", context)         # Variant(name, payload, enabled, feature_enabled) or None
engine.register_custom_strategies({"name": strategy_with_apply})
engine.count_toggle("my_flag", True)
engine.count_variant("my_flag", "variant_name")
engine.should_emit_impression_event("my_flag")
engine.get_metrics()                           # bucket dict for /client/metrics, or None
engine.get_state()                             # client-features JSON (the SDK caches it)
engine.list_known_toggles()                    # [FeatureDefinition(name, project, type)]
# impact metrics
engine.define_counter(name, help_text); engine.inc_counter(name, value=1, labels=None)
engine.define_gauge(name, help_text); engine.set_gauge(name, value, labels=None)
engine.define_histogram(name, help_text, buckets=None); engine.observe_histogram(name, value, labels=None)
engine.collect_impact_metrics(); engine.restore_impact_metrics(metrics)
```

## Building and testing locally

```bash
pip install maturin
maturin develop --release                          # builds + installs into the active venv
pip install "yggdrasil-engine==1.3.1" pytest       # the reference engine for the differential tests
CT_CLIENT_SPEC_PATH=/path/to/client-specification/specifications pytest tests/
```

The suite runs three layers:

- **Unit tests** for each method.
- **The Unleash client specification**, version 6.1.0.
- **Differential tests against yggdrasil-engine 1.3.1.** They cover randomized
  flag states, adversarial contexts, contexts shaped by the SDK, odd arguments,
  custom strategies, metrics, impact metrics and exception classes.

## Benchmark

`bench/bench.py` compares this binding against whichever `yggdrasil-engine` is
importable: the 1.x or 2.0 API, as a PyPI wheel or a local checkout on
`PYTHONPATH`. Rows are aligned so both engines do the same work per call.

```bash
pip install yggdrasil-engine
maturin develop --release
python bench/bench.py
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
ct-unleash-engine==0.2.0
```

## Using it as the SDK engine

`UnleashClient` 6.x creates its engine with `UnleashEngine()` from
`yggdrasil_engine.engine`. Either swap that import in a fork of the SDK
(`from ct_unleash_engine import UnleashEngine`), or run both engines side by side
and compare results before cutover. The SDK reports
`yggdrasil_engine.__yggdrasil_core_version__` to the Unleash server, and this
package exposes the same value, so registration payloads stay unchanged.
