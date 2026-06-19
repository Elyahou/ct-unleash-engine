//! PyO3 binding to `yggdrasil-core`.
//!
//! The evaluation hot path (`is_enabled`, `get_variant`) reads the context
//! straight from the Python `dict` via PyO3 and evaluates in Rust — no JSON
//! serialization on either side (unlike the official ctypes `yggdrasil-engine`,
//! which `json.dumps` the context on every call). Rare operations
//! (`take_state`, `get_state`, `get_metrics`, `list_known_toggles`) still use
//! JSON, which is fine because they are not per-evaluation.
//!
//! Because PyO3 touches Python objects, the GIL is held for the whole call, so
//! there is no GIL release/re-acquire — which also removes the p99 latency tail
//! the ctypes binding exhibits under thread contention.

use std::collections::HashMap;

use chrono::Utc;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use unleash_yggdrasil::state::EnrichedContext;
use unleash_yggdrasil::{Context, EngineState, UpdateMessage};

/// Pure-data engine handle. All evaluation is delegated to `yggdrasil-core`.
#[pyclass]
struct NativeEngine {
    state: EngineState,
}

#[pymethods]
impl NativeEngine {
    #[new]
    fn new() -> Self {
        NativeEngine {
            state: EngineState::default(),
        }
    }

    /// Apply a full or delta client-features payload (JSON).
    ///
    /// Returns a newline-joined warning string for unparseable toggles, or
    /// `None` when everything parsed. Raises `ValueError` only when the whole
    /// payload is not valid JSON / not a recognizable client-features shape.
    fn take_state(&mut self, json: &str) -> PyResult<Option<String>> {
        let message: UpdateMessage = serde_json::from_str(json)
            .map_err(|e| PyValueError::new_err(format!("invalid client features payload: {e}")))?;
        let warnings = self.state.take_state(message);
        Ok(warnings
            .map(|ws| {
                ws.iter()
                    .map(|w| format!("{}: {}", w.toggle_name, w.message))
                    .collect::<Vec<_>>()
                    .join("\n")
            })
            .filter(|s| !s.is_empty()))
    }

    /// Serialize the current state back to a client-features JSON string.
    fn get_state(&self) -> PyResult<String> {
        serde_json::to_string(&self.state.get_state())
            .map_err(|e| PyValueError::new_err(e.to_string()))
    }

    /// Evaluate a toggle. Returns `None` when the toggle is unknown (the SDK
    /// uses this to trigger its fallback), `Some(bool)` otherwise.
    fn is_enabled(&self, name: &str, context: &Bound<'_, PyDict>) -> Option<bool> {
        let ctx = pydict_to_context(context);
        let enriched = EnrichedContext::from(&ctx, name, None);
        self.state.check_enabled(&enriched)
    }

    /// Resolve a variant. Always returns a JSON object string with fields
    /// `name`, `enabled`, `feature_enabled`, and optional `payload`
    /// (the "disabled" variant when the toggle is off/unknown), matching the
    /// official engine's shape. The Python layer wraps it in a dataclass.
    fn get_variant(&self, name: &str, context: &Bound<'_, PyDict>) -> PyResult<String> {
        let ctx = pydict_to_context(context);
        let variant = self.state.get_variant(name, &ctx, &None);
        serde_json::to_string(&variant).map_err(|e| PyValueError::new_err(e.to_string()))
    }

    /// Record a toggle evaluation for the next metrics bucket.
    fn count_toggle(&self, name: &str, enabled: bool) {
        self.state.count_toggle(name, enabled);
    }

    /// Record a variant evaluation for the next metrics bucket.
    fn count_variant(&self, name: &str, variant: &str) {
        self.state.count_variant(name, variant);
    }

    /// Snapshot-and-reset the metrics bucket as a JSON string, or `None` when
    /// there is nothing to report.
    fn get_metrics(&mut self) -> PyResult<Option<String>> {
        match self.state.get_metrics(Utc::now()) {
            Some(bucket) => serde_json::to_string(&bucket)
                .map(Some)
                .map_err(|e| PyValueError::new_err(e.to_string())),
            None => Ok(None),
        }
    }

    /// Whether the toggle is configured to emit impression events.
    fn should_emit_impression_event(&self, name: &str) -> bool {
        self.state.should_emit_impression_event(name)
    }

    /// All known toggles as a JSON array string.
    fn list_known_toggles(&self) -> PyResult<String> {
        serde_json::to_string(&self.state.list_known_toggles())
            .map_err(|e| PyValueError::new_err(e.to_string()))
    }
}

/// Read a (already `_safe_context`-shaped) Python dict directly into a
/// yggdrasil `Context` — the zero-JSON hot path. Non-string values and a
/// non-dict `properties` are skipped rather than raising, mirroring the
/// permissive behavior of the SDK's context handling.
fn pydict_to_context(d: &Bound<'_, PyDict>) -> Context {
    let get_str = |key: &str| -> Option<String> {
        match d.get_item(key) {
            Ok(Some(v)) => v.extract::<String>().ok(),
            _ => None,
        }
    };

    let properties = match d.get_item("properties") {
        Ok(Some(v)) => v.downcast::<PyDict>().ok().map(|pd| {
            let mut m: HashMap<String, String> = HashMap::with_capacity(pd.len());
            for (k, val) in pd.iter() {
                if let (Ok(k), Ok(val)) = (k.extract::<String>(), val.extract::<String>()) {
                    m.insert(k, val);
                }
            }
            m
        }),
        _ => None,
    };

    Context {
        user_id: get_str("userId"),
        session_id: get_str("sessionId"),
        environment: get_str("environment"),
        app_name: get_str("appName"),
        current_time: get_str("currentTime"),
        remote_address: get_str("remoteAddress"),
        properties,
        ..Context::default()
    }
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<NativeEngine>()?;
    m.add("__core_version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
