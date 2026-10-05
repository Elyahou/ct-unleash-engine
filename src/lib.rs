//! PyO3 binding to yggdrasil-core that behaves exactly like yggdrasil-engine 1.3.1
//! (native library 0.20.7, core 0.21.3), only faster.
//!
//! The hot path (`is_enabled`, `get_variant`) reads a *plain* context straight from
//! the Python dict: an exact `dict` with exact `str` keys, whose values are `str`,
//! `None`, `bool` or an `int` that fits in 64 bits, and whose `properties` (if any)
//! has the same shape. Those values come out of 1.3.1's `json.dumps` + serde round
//! trip unchanged, so applying unleash-types' `Context::from_map` rules to them
//! directly yields the same context the reference engine evaluates.
//!
//! Anything else makes the fast methods return `NotImplemented`, and the Python
//! layer replays 1.3.1's exact steps instead: `json.dumps` in Python, then the
//! `*_json` / `*_bytes` methods below, which do what the native library does with
//! the same serde types and the same C-string handling. Exotic input (floats,
//! containers, dict or key subclasses, non-str keys, a nested `context` key, strings
//! that are not valid UTF-8) is therefore exact by construction, not by
//! re-implementation.

// PyO3 0.22's #[pymethods] expansion converts PyErr into itself, which clippy
// reports at each `PyResult` return type; the code itself has no such conversion.
#![allow(clippy::useless_conversion)]

use std::borrow::Cow;
use std::collections::HashMap;
use std::fmt;
use std::panic::{self, AssertUnwindSafe};
use std::sync::{Mutex, MutexGuard};

use chrono::Utc;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyBool, PyDict, PyLong, PyString, PyType};

use unleash_yggdrasil::impact_metrics::{
    BucketMetricOptions, CollectedMetric, MetricLabels, MetricOptions,
};
use unleash_yggdrasil::state::{EnrichedContext, ExternalResultsRef, PropertiesCow, PropertiesRef};
use unleash_yggdrasil::{Context, EngineState, ExtendedVariantDef, UpdateMessage};

const CONTEXT_FIELDS: [&str; 6] = [
    "userId",
    "sessionId",
    "environment",
    "appName",
    "currentTime",
    "remoteAddress",
];

/// The class 1.3.1 raises for native errors (`yggdrasil_engine.engine.YggdrasilError`
/// whenever that package is importable). Registered once from Python at import time.
static ERROR_CLASS: GILOnceCell<Py<PyType>> = GILOnceCell::new();

#[pyfunction]
fn _set_error_class(py: Python<'_>, cls: Bound<'_, PyType>) {
    let _ = ERROR_CLASS.set(py, cls.unbind());
}

/// The native library's error kinds, with its exact messages.
enum FfiError {
    Utf8,
    Null,
    InvalidJson(String),
    Panic,
}

impl fmt::Display for FfiError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            FfiError::Utf8 => write!(f, "Detected a non UTF-8 string in the input, this is a serious issue and you should report this as a bug."),
            FfiError::Null => write!(f, "Null error detected, this is a serious issue and you should report this as a bug."),
            FfiError::InvalidJson(message) => write!(f, "Failed to parse JSON: {message}"),
            FfiError::Panic => write!(f, "Engine panicked while processing the request. Please report this as a bug with the accompanying stack trace if available."),
        }
    }
}

fn raise(py: Python<'_>, err: FfiError) -> PyErr {
    match ERROR_CLASS.get(py) {
        Some(cls) => PyErr::from_type_bound(cls.bind(py).clone(), err.to_string()),
        None => PyRuntimeError::new_err(err.to_string()),
    }
}

/// Like the native library, a panic inside the engine becomes an error result
/// instead of unwinding into Python.
fn guard<T>(action: impl FnOnce() -> Result<T, FfiError>) -> Result<T, FfiError> {
    panic::catch_unwind(AssertUnwindSafe(action)).unwrap_or(Err(FfiError::Panic))
}

/// What the native library sees for a `c_char_p` argument: NULL for `None`,
/// otherwise the bytes up to the first NUL, which must be UTF-8.
fn c_str(raw: Option<&[u8]>) -> Result<&str, FfiError> {
    let raw = raw.ok_or(FfiError::Null)?;
    let end = raw.iter().position(|&b| b == 0).unwrap_or(raw.len());
    std::str::from_utf8(&raw[..end]).map_err(|_| FfiError::Utf8)
}

fn parse_json<T: serde::de::DeserializeOwned>(raw: Option<&[u8]>) -> Result<T, FfiError> {
    serde_json::from_str(c_str(raw)?).map_err(|e| FfiError::InvalidJson(e.to_string()))
}

/// Cut a string at its first NUL, as passing it through a C string would.
fn until_nul(s: Cow<'_, str>) -> Cow<'_, str> {
    match s.find('\0') {
        None => s,
        Some(end) => match s {
            Cow::Borrowed(b) => Cow::Borrowed(&b[..end]),
            Cow::Owned(mut o) => {
                o.truncate(end);
                Cow::Owned(o)
            }
        },
    }
}

/// A toggle/variant name as the native library receives it, or `None` when the
/// Python slow path must produce it (not an exact `str`, or not UTF-8 encodable),
/// so that `.encode("utf-8")` raises exactly what 1.3.1 raises.
fn fast_name<'a>(name: &'a Bound<'_, PyAny>) -> Option<Cow<'a, str>> {
    let name = name.downcast_exact::<PyString>().ok()?;
    name.to_cow().ok().map(until_nul)
}

/// ctypes converts a `c_bool` argument with `PyObject_IsTrue` and wraps a failure in
/// `ctypes.ArgumentError`; mirror both so odd fallback values behave the same.
fn c_bool(py: Python<'_>, value: &Bound<'_, PyAny>, position: usize) -> PyResult<bool> {
    value.is_truthy().map_err(|cause| {
        let message = format!("argument {position}: {cause}");
        match py
            .import_bound("ctypes")
            .and_then(|m| m.getattr("ArgumentError"))
            .and_then(|cls| Ok(cls.downcast_into::<PyType>()?))
        {
            Ok(cls) => PyErr::from_type_bound(cls, message),
            Err(_) => cause,
        }
    })
}

/// The context is outside the fast path's shape; the caller must take the exact path.
struct NotPlain;

/// A plain value after 1.3.1's `json.dumps` + unleash-types' `parse_value`:
/// strings stay as they are, `None` is dropped, bools and 64-bit ints are stringified.
fn plain_value<'a>(value: &'a Bound<'_, PyAny>) -> Result<Option<Cow<'a, str>>, NotPlain> {
    if let Ok(s) = value.downcast::<PyString>() {
        // json.dumps writes a str subclass's underlying data, which is what to_cow reads.
        return s.to_cow().map(Some).map_err(|_| NotPlain);
    }
    if value.is_none() {
        return Ok(None);
    }
    if let Ok(b) = value.downcast_exact::<PyBool>() {
        return Ok(Some(Cow::Borrowed(if b.is_true() {
            "true"
        } else {
            "false"
        })));
    }
    if value.is_instance_of::<PyLong>() {
        // json.dumps writes int.__repr__ and serde keeps 64-bit integers exact. Wider
        // integers turn into floats inside serde, so they go to the exact path.
        if let Ok(v) = value.extract::<i64>() {
            return Ok(Some(Cow::Owned(v.to_string())));
        }
        if let Ok(v) = value.extract::<u64>() {
            return Ok(Some(Cow::Owned(v.to_string())));
        }
    }
    Err(NotPlain)
}

type Items<'py> = Vec<(Bound<'py, PyAny>, Bound<'py, PyAny>)>;

/// Build the `EnrichedContext` for a plain context and hand it to `evaluate`.
///
/// Applies unleash-types' `Context::from_map` rules: values come from `properties`
/// first; each known field is read from the top level (even when `None` there) and
/// otherwise moved out of `properties`; remaining top-level *strings* are merged into
/// `properties`, overriding same-named entries. Strings are borrowed from Python
/// wherever the build allows it (see `PyStringMethods::to_cow`).
fn with_plain_context<R>(
    ctx: &Bound<'_, PyDict>,
    toggle_name: &str,
    custom_results: &HashMap<String, bool>,
    evaluate: impl FnOnce(&EnrichedContext<'_>) -> R,
) -> Result<R, NotPlain> {
    // Collect items up front: every borrowed &str below points into these objects.
    let top: Items<'_> = ctx.iter().collect();

    let mut properties_value = None;
    let mut field_values: [Option<&Bound<'_, PyAny>>; 6] = [None; 6];
    let mut leftovers = Vec::new();
    for (key, value) in &top {
        // Exact str keys only: a subclass could hash or compare differently.
        let key = key.downcast_exact::<PyString>().map_err(|_| NotPlain)?;
        let key = key.to_cow().map_err(|_| NotPlain)?;
        match key.as_ref() {
            // from_map evaluates the nested object instead; rare, so take the exact path.
            "context" => return Err(NotPlain),
            "properties" => properties_value = Some(value),
            name => match CONTEXT_FIELDS.iter().position(|field| *field == name) {
                Some(index) => field_values[index] = Some(value),
                None => leftovers.push((key, value)),
            },
        }
    }

    let property_items: Items<'_> = match properties_value {
        Some(value) => match value.downcast_exact::<PyDict>() {
            Ok(dict) => dict.iter().collect(),
            // A dict subclass may serialise through an overridden items().
            Err(_) if value.is_instance_of::<PyDict>() => return Err(NotPlain),
            // Not an object: from_map ignores it, but it must still be plain.
            Err(_) => {
                plain_value(value)?;
                Vec::new()
            }
        },
        None => Vec::new(),
    };

    let mut properties: PropertiesCow<'_> =
        HashMap::with_capacity(property_items.len() + leftovers.len());
    for (key, value) in &property_items {
        let key = key.downcast_exact::<PyString>().map_err(|_| NotPlain)?;
        let key = key.to_cow().map_err(|_| NotPlain)?;
        if let Some(value) = plain_value(value)? {
            properties.insert(key, value);
        }
    }

    let mut fields: [Option<Cow<'_, str>>; 6] = Default::default();
    for (index, slot) in fields.iter_mut().enumerate() {
        *slot = match field_values[index] {
            Some(value) => plain_value(value)?,
            None => properties.remove(CONTEXT_FIELDS[index]),
        };
    }

    for (key, value) in leftovers {
        match value.downcast::<PyString>() {
            Ok(s) => {
                properties.insert(key, s.to_cow().map_err(|_| NotPlain)?);
            }
            // Only strings are merged; anything else is dropped but must be plain.
            Err(_) => {
                plain_value(value)?;
            }
        }
    }

    let enriched = EnrichedContext {
        user_id: fields[0].as_deref(),
        session_id: fields[1].as_deref(),
        environment: fields[2].as_deref(),
        app_name: fields[3].as_deref(),
        current_time: fields[4].as_deref(),
        remote_address: fields[5].as_deref(),
        properties: if properties.is_empty() {
            None
        } else {
            Some(PropertiesRef::Cows(&properties))
        },
        external_results: Some(ExternalResultsRef::Strings(custom_results)),
        toggle_name,
        runtime_hostname: None,
    };
    Ok(evaluate(&enriched))
}

/// `check_variant` composed exactly as the native library composes it.
fn variant_response(
    engine: &EngineState,
    context: &EnrichedContext<'_>,
) -> Option<ExtendedVariantDef> {
    let base_variant = engine.check_variant(context);
    let toggle_enabled = engine.check_enabled(context).unwrap_or_default();
    base_variant.map(|variant| variant.to_enriched_response(toggle_enabled))
}

/// `(name, payload_type, payload_value, enabled, feature_enabled)`, or `None` when
/// the engine has no variant (1.3.1 returns `None` to the SDK then).
fn variant_to_py(py: Python<'_>, variant: Option<ExtendedVariantDef>) -> PyObject {
    match variant {
        None => py.None(),
        Some(v) => {
            let (payload_type, payload_value) = match v.payload {
                Some(p) => (Some(p.payload_type), Some(p.value)),
                None => (None, None),
            };
            (
                v.name,
                payload_type,
                payload_value,
                v.enabled,
                v.feature_enabled,
            )
                .into_py(py)
        }
    }
}

#[pyclass(frozen)]
struct NativeEngine {
    state: Mutex<EngineState>,
}

impl NativeEngine {
    /// Same poison recovery as the native library: a panic mid-call must not wedge
    /// the engine for later calls.
    fn lock(&self) -> MutexGuard<'_, EngineState> {
        self.state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

#[pymethods]
impl NativeEngine {
    #[new]
    fn new() -> Self {
        NativeEngine {
            state: Mutex::new(EngineState::default()),
        }
    }

    // --- state ------------------------------------------------------------------

    /// 1.3.1 never surfaces the native result here: a payload that fails to parse
    /// leaves the state untouched, and warnings come back as an error status the
    /// Python side does not check. Ignoring both is deliberate.
    #[pyo3(signature = (json))]
    fn take_state(&self, json: Option<&[u8]>) {
        let _ = guard(|| {
            let mut engine = self.lock();
            let message: UpdateMessage = parse_json(json)?;
            let _ = engine.take_state(message);
            Ok(())
        });
    }

    /// Client-features JSON; the Python layer re-serialises it with `json.dumps`
    /// exactly like 1.3.1. `None` where 1.3.1 would see an error response.
    fn get_state(&self) -> Option<String> {
        guard(|| Ok(serde_json::to_string(&self.lock().get_state()).ok()))
            .ok()
            .flatten()
    }

    // --- evaluation, fast path ---------------------------------------------------

    /// `True` / `False` / `None` (unknown toggle), or `NotImplemented` when the
    /// name or context is outside the fast path.
    fn is_enabled(
        &self,
        py: Python<'_>,
        name: &Bound<'_, PyAny>,
        context: &Bound<'_, PyAny>,
    ) -> PyResult<PyObject> {
        let (Some(name), Ok(context)) = (fast_name(name), context.downcast_exact::<PyDict>())
        else {
            return Ok(py.NotImplemented());
        };
        let no_custom_results = HashMap::new();
        let outcome = guard(|| {
            Ok(with_plain_context(
                context,
                &name,
                &no_custom_results,
                |enriched| self.lock().check_enabled(enriched),
            ))
        });
        match outcome {
            Ok(Ok(enabled)) => Ok(enabled.into_py(py)),
            Ok(Err(NotPlain)) => Ok(py.NotImplemented()),
            Err(e) => Err(raise(py, e)),
        }
    }

    /// See `variant_to_py`; `NotImplemented` when outside the fast path.
    fn get_variant(
        &self,
        py: Python<'_>,
        name: &Bound<'_, PyAny>,
        context: &Bound<'_, PyAny>,
    ) -> PyResult<PyObject> {
        let (Some(name), Ok(context)) = (fast_name(name), context.downcast_exact::<PyDict>())
        else {
            return Ok(py.NotImplemented());
        };
        let no_custom_results = HashMap::new();
        let outcome = guard(|| {
            Ok(with_plain_context(
                context,
                &name,
                &no_custom_results,
                |enriched| variant_response(&self.lock(), enriched),
            ))
        });
        match outcome {
            Ok(Ok(variant)) => Ok(variant_to_py(py, variant)),
            Ok(Err(NotPlain)) => Ok(py.NotImplemented()),
            Err(e) => Err(raise(py, e)),
        }
    }

    // --- evaluation, exact path (the native library's own steps) ----------------

    #[pyo3(signature = (name, context, custom_results))]
    fn is_enabled_json(
        &self,
        py: Python<'_>,
        name: Option<&[u8]>,
        context: Option<&[u8]>,
        custom_results: Option<&[u8]>,
    ) -> PyResult<Option<bool>> {
        guard(|| {
            let engine = self.lock();
            let name = c_str(name)?;
            let context: Context = parse_json(context)?;
            let custom_results: Option<HashMap<String, bool>> = match custom_results {
                None => None,
                raw => Some(parse_json(raw)?),
            };
            let enriched = EnrichedContext::from(&context, name, custom_results.as_ref());
            Ok(engine.check_enabled(&enriched))
        })
        .map_err(|e| raise(py, e))
    }

    #[pyo3(signature = (name, context, custom_results))]
    fn get_variant_json(
        &self,
        py: Python<'_>,
        name: Option<&[u8]>,
        context: Option<&[u8]>,
        custom_results: Option<&[u8]>,
    ) -> PyResult<PyObject> {
        let variant = guard(|| {
            let engine = self.lock();
            let name = c_str(name)?;
            let context: Context = parse_json(context)?;
            let custom_results: Option<HashMap<String, bool>> = match custom_results {
                None => None,
                raw => Some(parse_json(raw)?),
            };
            let enriched = EnrichedContext::from(&context, name, custom_results.as_ref());
            Ok(variant_response(&engine, &enriched))
        })
        .map_err(|e| raise(py, e))?;
        Ok(variant_to_py(py, variant))
    }

    // --- metrics -------------------------------------------------------------------

    /// Native errors are ignored here, as in 1.3.1 (it frees the response unread).
    /// Returns `NotImplemented` when the name needs the Python slow path.
    fn count_toggle(
        &self,
        py: Python<'_>,
        name: &Bound<'_, PyAny>,
        enabled: &Bound<'_, PyAny>,
    ) -> PyResult<PyObject> {
        let Some(name) = fast_name(name) else {
            return Ok(py.NotImplemented());
        };
        let enabled = c_bool(py, enabled, 3)?;
        let _ = guard(|| {
            self.lock().count_toggle(&name, enabled);
            Ok(())
        });
        Ok(py.None())
    }

    #[pyo3(signature = (name, enabled))]
    fn count_toggle_bytes(
        &self,
        py: Python<'_>,
        name: Option<&[u8]>,
        enabled: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let enabled = c_bool(py, enabled, 3)?;
        let _ = guard(|| {
            let engine = self.lock();
            engine.count_toggle(c_str(name)?, enabled);
            Ok(())
        });
        Ok(())
    }

    fn count_variant(
        &self,
        py: Python<'_>,
        name: &Bound<'_, PyAny>,
        variant: &Bound<'_, PyAny>,
    ) -> PyObject {
        let (Some(name), Some(variant)) = (fast_name(name), fast_name(variant)) else {
            return py.NotImplemented();
        };
        let _ = guard(|| {
            self.lock().count_variant(&name, &variant);
            Ok(())
        });
        py.None()
    }

    #[pyo3(signature = (name, variant))]
    fn count_variant_bytes(&self, name: Option<&[u8]>, variant: Option<&[u8]>) {
        let _ = guard(|| {
            let engine = self.lock();
            let name = c_str(name)?;
            let variant = c_str(variant)?;
            engine.count_variant(name, variant);
            Ok(())
        });
    }

    /// Snapshot-and-reset the metrics bucket as JSON, or `None` when empty.
    fn get_metrics(&self, py: Python<'_>) -> PyResult<Option<String>> {
        guard(|| Ok(self.lock().get_metrics(Utc::now())))
            .map(|bucket| bucket.and_then(|b| serde_json::to_string(&b).ok()))
            .map_err(|e| raise(py, e))
    }

    fn should_emit_impression_event(
        &self,
        py: Python<'_>,
        name: &Bound<'_, PyAny>,
    ) -> PyResult<PyObject> {
        let Some(name) = fast_name(name) else {
            return Ok(py.NotImplemented());
        };
        guard(|| Ok(self.lock().should_emit_impression_event(&name)))
            .map(|emit| emit.into_py(py))
            .map_err(|e| raise(py, e))
    }

    #[pyo3(signature = (name))]
    fn should_emit_impression_event_bytes(
        &self,
        py: Python<'_>,
        name: Option<&[u8]>,
    ) -> PyResult<bool> {
        guard(|| {
            let engine = self.lock();
            Ok(engine.should_emit_impression_event(c_str(name)?))
        })
        .map_err(|e| raise(py, e))
    }

    fn list_known_toggles(&self, py: Python<'_>) -> PyResult<String> {
        guard(|| {
            serde_json::to_string(&self.lock().list_known_toggles())
                .map_err(|e| FfiError::InvalidJson(e.to_string()))
        })
        .map_err(|e| raise(py, e))
    }

    // --- impact metrics (same arguments the native library receives) -------------

    #[pyo3(signature = (name, help))]
    fn define_counter(
        &self,
        py: Python<'_>,
        name: Option<&[u8]>,
        help: Option<&[u8]>,
    ) -> PyResult<()> {
        guard(|| {
            let engine = self.lock();
            engine.define_counter(MetricOptions::new(c_str(name)?, c_str(help)?));
            Ok(())
        })
        .map_err(|e| raise(py, e))
    }

    #[pyo3(signature = (name, value, labels))]
    fn inc_counter(
        &self,
        py: Python<'_>,
        name: Option<&[u8]>,
        value: i64,
        labels: Option<&[u8]>,
    ) -> PyResult<()> {
        guard(|| {
            let engine = self.lock();
            let name = c_str(name)?;
            match labels {
                None => engine.inc_counter_by(name, value),
                raw => {
                    let labels: MetricLabels = parse_json(raw)?;
                    engine.inc_counter_with_labels(name, value, &labels);
                }
            }
            Ok(())
        })
        .map_err(|e| raise(py, e))
    }

    fn collect_impact_metrics(&self, py: Python<'_>) -> PyResult<String> {
        guard(|| {
            serde_json::to_string(&self.lock().collect_impact_metrics())
                .map_err(|e| FfiError::InvalidJson(e.to_string()))
        })
        .map_err(|e| raise(py, e))
    }

    #[pyo3(signature = (metrics))]
    fn restore_impact_metrics(&self, py: Python<'_>, metrics: Option<&[u8]>) -> PyResult<()> {
        guard(|| {
            let engine = self.lock();
            let metrics: Vec<CollectedMetric> = parse_json(metrics)?;
            engine.restore_impact_metrics(metrics);
            Ok(())
        })
        .map_err(|e| raise(py, e))
    }

    #[pyo3(signature = (name, help))]
    fn define_gauge(
        &self,
        py: Python<'_>,
        name: Option<&[u8]>,
        help: Option<&[u8]>,
    ) -> PyResult<()> {
        guard(|| {
            let engine = self.lock();
            engine.define_gauge(MetricOptions::new(c_str(name)?, c_str(help)?));
            Ok(())
        })
        .map_err(|e| raise(py, e))
    }

    #[pyo3(signature = (name, value, labels))]
    fn set_gauge(
        &self,
        py: Python<'_>,
        name: Option<&[u8]>,
        value: f64,
        labels: Option<&[u8]>,
    ) -> PyResult<()> {
        guard(|| {
            let engine = self.lock();
            let name = c_str(name)?;
            match labels {
                None => engine.set_gauge(name, value),
                raw => {
                    let labels: MetricLabels = parse_json(raw)?;
                    engine.set_gauge_with_labels(name, value, &labels);
                }
            }
            Ok(())
        })
        .map_err(|e| raise(py, e))
    }

    #[pyo3(signature = (name, help, buckets))]
    fn define_histogram(
        &self,
        py: Python<'_>,
        name: Option<&[u8]>,
        help: Option<&[u8]>,
        buckets: Option<&[u8]>,
    ) -> PyResult<()> {
        guard(|| {
            let engine = self.lock();
            let name = c_str(name)?;
            let help = c_str(help)?;
            let buckets: Vec<f64> = parse_json(buckets)?;
            engine.define_histogram(BucketMetricOptions::new(name, help, buckets));
            Ok(())
        })
        .map_err(|e| raise(py, e))
    }

    #[pyo3(signature = (name, value, labels))]
    fn observe_histogram(
        &self,
        py: Python<'_>,
        name: Option<&[u8]>,
        value: f64,
        labels: Option<&[u8]>,
    ) -> PyResult<()> {
        guard(|| {
            let engine = self.lock();
            let name = c_str(name)?;
            match labels {
                None => engine.observe_histogram(name, value),
                raw => {
                    let labels: MetricLabels = parse_json(raw)?;
                    engine.observe_histogram_with_labels(name, value, &labels);
                }
            }
            Ok(())
        })
        .map_err(|e| raise(py, e))
    }
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<NativeEngine>()?;
    m.add_function(wrap_pyfunction!(_set_error_class, m)?)?;
    m.add("__core_version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
