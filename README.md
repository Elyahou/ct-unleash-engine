# ct-unleash-engine

A fast [PyO3](https://pyo3.rs) binding to [`yggdrasil-core`](https://crates.io/crates/unleash-yggdrasil), the Rust evaluation engine behind Unleash.

Unlike the official `yggdrasil-engine` (which uses `ctypes` and `json.dumps`-es the
context on every `is_enabled` call), this binding reads the context **directly from
the Python `dict`** via PyO3 — no JSON serialization on the hot path. It also holds
the GIL for the whole call, so it has no GIL release/re-acquire and therefore no
p99 latency tail under thread contention.

Measured on representative flags (Python 3.13): **~8x faster than the official
ctypes engine**, with identical evaluation results.

## API

`UnleashEngine` is a drop-in for the engine the Unleash Python SDK uses:

```python
from ct_unleash_engine import UnleashEngine

engine = UnleashEngine()
engine.take_state(client_features_json)          # full or delta payload
engine.is_enabled("my_flag", {"userId": "42", "properties": {"plan": "pro"}})
engine.get_variant("my_flag", context)           # -> Variant(name, enabled, feature_enabled, payload)
engine.count_toggle("my_flag", True)
engine.get_metrics()                              # bucket dict for /client/metrics, or None
engine.get_state()                               # serialize state (streaming cache)
```

`is_enabled` returns `None` for unknown toggles (the SDK's fallback signal).
Custom strategies are not supported (`register_custom_strategies` raises).

## Building locally

```bash
pip install maturin
maturin develop --release       # builds + installs into the active venv
pytest tests/                   # unit + spec-suite + differential vs the official engine
```

## Production wheels

The binding is built with `abi3-py38`, so **one wheel per platform** works on
CPython 3.8+. CI (`.github/workflows/wheels.yml`) builds `manylinux` (x86_64 +
aarch64) and macOS wheels via `maturin`. Your deploy target's platform must
match the wheel — for Linux pods you need the `manylinux` wheel, not a local
macOS build.

Install in an application:

```
# requirements.txt (from your private index, or pinned git tag)
ct-unleash-engine==0.1.0
```

## Using it as the SDK engine

Either swap the import in a fork of the SDK
(`from ct_unleash_engine import UnleashEngine`) or run it in shadow alongside
the official engine and compare results before cutover.
