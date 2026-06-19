"""Runs the official Unleash client-specification suite through the engine.

Locates the specs via the CT_CLIENT_SPEC_PATH env var, or a sibling
unleash-python-sdk checkout. Skips when the specs aren't present.
"""

import json
import os
from pathlib import Path

import pytest

from ct_unleash_engine import UnleashEngine


def _spec_dir():
    env = os.environ.get("CT_CLIENT_SPEC_PATH")
    candidates = [env] if env else []
    here = Path(__file__).resolve()
    candidates += [
        here.parents[2]
        / "unleash-python-sdk"
        / "tests"
        / "specification_tests"
        / "client-specification"
        / "specifications",
    ]
    for c in candidates:
        if c and Path(c).is_dir():
            return Path(c)
    return None


SPEC_DIR = _spec_dir()
pytestmark = pytest.mark.skipif(
    SPEC_DIR is None,
    reason="client-specification not found (set CT_CLIENT_SPEC_PATH)",
)


def _load_cases():
    cases = []
    index = json.loads((SPEC_DIR / "index.json").read_text())
    for spec_file in index:
        data = json.loads((SPEC_DIR / spec_file).read_text())
        state = json.dumps(data["state"])
        for t in data.get("tests") or []:
            cases.append((data["name"], "enabled", state, t))
        for t in data.get("variantTests") or []:
            cases.append((data["name"], "variant", state, t))
    return cases


CASES = _load_cases() if SPEC_DIR else []


@pytest.mark.parametrize(
    "name,kind,state,test",
    CASES,
    ids=[f"{c[0]}:{c[1]}:{c[3]['description']}" for c in CASES] or None,
)
def test_client_specification(name, kind, state, test):
    engine = UnleashEngine()
    engine.take_state(state)
    context = test.get("context") or {}
    if kind == "enabled":
        result = engine.is_enabled(test["toggleName"], context)
        # The spec treats an unknown toggle's expected value as False; the
        # engine returns None there (the SDK's fallback signal).
        assert (result or False) == test["expectedResult"]
    else:
        expected = test["expectedResult"]
        v = engine.get_variant(test["toggleName"], context)
        assert v.name == expected["name"]
        assert v.enabled == expected["enabled"]
        if "feature_enabled" in expected:
            assert v.feature_enabled == expected["feature_enabled"]
        assert (v.payload or None) == (expected.get("payload") or None)
