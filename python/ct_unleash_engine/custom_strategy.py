# Verbatim copy of yggdrasil_engine/custom_strategy.py from yggdrasil-engine 2.0.0,
# so custom strategies are collected and evaluated exactly as the reference engine
# does. That includes its quirks (for example `applicationHostname` is missing from
# _STANDARD_STRATEGIES, and a toggle without "strategies" raises KeyError). They are
# kept on purpose: changing anything here makes results diverge from 2.0.0.
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

import inspect
import json
from typing import Dict

_STANDARD_STRATEGIES = [
    "default",
    "userWithId",
    "gradualRolloutUserId",
    "gradualRolloutSessionId",
    "gradualRolloutRandom",
    "flexibleRollout",
    "remoteAddress",
]


def get_features_json(message):
    if "features" in message:
        return message["features"]
    elif "events" in message:
        features = {}
        for event in message["events"]:
            if event["type"] == "feature-updated":
                feature = event["feature"]
                features[feature["name"]] = feature
            elif event["type"] == "feature-removed":
                features.pop(event["featureName"], None)
            elif event["type"] == "hydration":
                features = {feature["name"]: feature for feature in event["features"]}
        return list(features.values())


class CustomStrategyHandler:
    def __init__(self):
        self.strategy_definitions = {}
        self.strategy_implementations = {}

    def update_strategies(self, features_json: str):
        custom_strategies = {}
        parsed_toggles = json.loads(features_json)

        for toggle in get_features_json(parsed_toggles):
            toggle_name = toggle["name"]
            for strategy in toggle["strategies"]:
                if strategy["name"] not in _STANDARD_STRATEGIES:
                    custom_strategies[toggle_name] = strategy["name"]

            toggle_strategies = [
                strategy
                for strategy in toggle["strategies"]
                if strategy["name"] not in _STANDARD_STRATEGIES
            ]
            if toggle_strategies:
                custom_strategies[toggle_name] = toggle_strategies

        self.strategy_definitions = custom_strategies

    def register_custom_strategies(self, custom_strategies: Dict[str, any]):
        for strategy_name, strategy in custom_strategies.items():
            if hasattr(strategy, "apply"):
                apply_method = strategy.apply
                signature = inspect.signature(apply_method)
                parameters = list(signature.parameters)
                if len(parameters) != 2:
                    raise ValueError(
                        f"Custom strategy '{strategy_name}' does not have an apply method "
                        f"with exactly two parameters. Found {len(parameters)}."
                    )
                self.strategy_implementations[strategy_name] = strategy
            else:
                raise ValueError(
                    f"Custom strategy {strategy_name} does not have an apply method"
                )

    def evaluate_custom_strategies(
        self, toggle_name: str, context: dict
    ) -> Dict[str, bool]:
        results = {}
        for index, strategy in enumerate(
            self.strategy_definitions.get(toggle_name, [])
        ):
            key = f"customStrategy{index + 1}"
            strategy_impl = self.strategy_implementations.get(strategy["name"])
            result = (
                strategy_impl.apply(strategy["parameters"], context)
                if strategy_impl
                else False
            )
            results[key] = result

        return results
