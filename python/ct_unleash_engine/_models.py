# Verbatim copy of the result and definition classes from yggdrasil_engine/engine.py
# in yggdrasil-engine 2.0.0, so results compare, print and serialise exactly like
# the reference engine's. That includes FeatureToggle refusing to be used as a
# bool. Keep them byte for byte: changing anything here makes results diverge.
#
# Original work: https://github.com/Unleash/yggdrasil-bindings (python-engine)
#
# MIT License
#
# Copyright (c) 2023 Unleash
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(init=False)
class FeatureToggle:
    """`FeatureToggle` is the result of querying if a feature is enabled."""

    name: str
    """The name of the toggle that was queried."""

    is_enabled: bool = False
    """Whether the feature is enabled for the given context.

    Defaults to `False` when the engine did not know the toggle and no fallback
    resolved a value, or when the evaluation failed.
    """

    is_found: bool = False
    """Whether the engine knew about the toggle at all.

    `False` means the toggle was missing from the engine's state or evaluation
    errored. That is distinct from a known toggle that evaluated to disabled,
    which is `is_found=True, is_enabled=False`.
    """

    requires_impression_event_emission: bool = False
    """Whether the engine expects its caller to emit an impression event.

    These bindings are not concerned with the publishing itself. However,
    the engine is the source of whether a toggle has the publication of
    impression events enabled.

    `False` means that the SDK should not emit impression events. It also means
    the engine could not be asked, either because the lookup itself failed or
    because an earlier step of the evaluation did."""

    def __init__(
        self,
        *,
        name: str,
        is_enabled: bool = False,
        is_found: bool = False,
        requires_impression_event_emission: bool = False,
    ):
        self.name = name
        self.is_enabled = is_enabled
        self.is_found = is_found
        self.requires_impression_event_emission = requires_impression_event_emission

    def __bool__(self):
        raise TypeError(
            f"FeatureToggle for {self.name!r} has no truth value. "
            "Read .is_enabled to check whether the feature is enabled, "
            "or .is_found to check whether the engine knew the toggle."
        )


@dataclass
class Variant:
    name: str
    payload: Optional[Dict[str, str]] = field(default_factory=dict)
    enabled: bool = False
    feature_enabled: bool = False

    @staticmethod
    def from_dict(data: dict) -> "Variant":
        return Variant(
            name=data.get("name", ""),
            payload=data.get("payload"),
            enabled=data.get("enabled", False),
            feature_enabled=data.get("featureEnabled", False),
        )


def disabled_variant() -> Variant:
    return Variant(name="disabled", payload=None, enabled=False, feature_enabled=False)


"""The variant the engine falls back to when no variant could be resolved.

`payload` is `None` rather than an empty dict so that it drops out of
serialization, matching what the engine itself hands back."""


@dataclass(frozen=True)
class FeatureVariant:
    """`FeatureVariant` is the result of querying which variant a feature resolves to."""

    name: str
    """The name of the toggle that was queried."""

    variant: Variant
    """The variant the toggle resolved to for the given context.

    `get_variant` hands back the disabled variant when the engine did not know
    the toggle, or when the evaluation failed. Note that a known toggle can
    resolve to a variant that looks exactly like it, so read `is_found` to tell
    the two apart.
    """

    is_found: bool
    """Whether the engine knew about the toggle at all.

    `False` means the toggle was missing from the engine's state or evaluation
    errored. That is distinct from a known toggle that resolved to the disabled
    variant, which is `is_found=True`.
    """

    requires_impression_event_emission: bool
    """Whether the engine expects its caller to emit an impression event.

    These bindings are not concerned with the publishing itself. However,
    the engine is the source of whether a toggle has the publication of
    impression events enabled.

    `False` means that the SDK should not emit impression events. It also means
    the engine could not be asked, either because the lookup itself failed or
    because an earlier step of the evaluation did."""


@dataclass
class FeatureDefinition:
    name: str
    project: str
    type: Optional[str]

    @staticmethod
    def from_dict(data: dict) -> "FeatureDefinition":
        return FeatureDefinition(
            name=data.get("name", ""),
            project=data.get("project", ""),
            type=data.get("type"),
        )


def load_feature_defs(raw_defs: List[dict]) -> List[FeatureDefinition]:
    return [FeatureDefinition.from_dict(defn) for defn in raw_defs]
