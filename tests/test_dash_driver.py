"""Exercise the real Dash HTTP callback protocol without JavaScript or live services."""

import json
from typing import Any

from dash import Dash


def identity(value: Any) -> str:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"))
        if isinstance(value, dict)
        else str(value)
    )


def matches(pattern: Any, candidate: Any) -> bool:
    if isinstance(pattern, str) and pattern.startswith("{"):
        pattern = json.loads(pattern)
    if isinstance(pattern, dict):
        return isinstance(candidate, dict) and all(
            isinstance(value, list) or candidate.get(key) == value for key, value in pattern.items()
        )
    return bool(pattern == candidate)


class DashDriver:
    def __init__(self, app: Dash) -> None:
        self.app = app
        self.client = app.server.test_client()
        response = self.client.get("/_dash-layout")
        assert response.status_code == 200
        self.tree: Any = response.get_json()

    def nodes(self, tree: Any = None) -> list[dict[str, Any]]:
        tree = self.tree if tree is None else tree
        if isinstance(tree, list):
            return [node for child in tree for node in self.nodes(child)]
        if isinstance(tree, dict) and "props" in tree:
            children = tree["props"].get("children", [])
            return [tree, *self.nodes(children)] if children is not None else [tree]
        return []

    def props(self, component: Any) -> dict[str, Any]:
        return next(
            node["props"]
            for node in self.nodes()
            if identity(node["props"].get("id")) == identity(component)
        )

    def get(self, component: Any, prop: str, default: Any = None) -> Any:
        return self.props(component).get(prop, default)

    def set(self, component: Any, prop: str, value: Any) -> None:
        self.props(component)[prop] = value

    def call(self, output: str, trigger: Any, prop: str = "n_clicks", value: Any = 1) -> Any:
        self.set(trigger, prop, value)
        candidates = [
            (key, item)
            for key, item in self.app.callback_map.items()
            if output in key
            and "callback" in item
            and any(
                dep["property"] == prop and matches(dep["id"], trigger) for dep in item["inputs"]
            )
        ]
        assert len(candidates) == 1, [key for key, _ in candidates]
        key, callback = candidates[0]

        def dependency(dep: dict[str, Any]) -> Any:
            component = dep["id"]
            if isinstance(component, str) and component.startswith("{"):
                return [
                    {
                        "id": node["props"]["id"],
                        "property": dep["property"],
                        "value": node["props"].get(dep["property"]),
                    }
                    for node in self.nodes()
                    if matches(component, node["props"].get("id"))
                ]
            return {
                "id": component,
                "property": dep["property"],
                "value": self.get(component, dep["property"]),
            }

        outputs = callback["output"]
        if isinstance(outputs, list):
            outputs = [
                {"id": item.component_id, "property": item.component_property} for item in outputs
            ]
        else:
            outputs = {"id": outputs.component_id, "property": outputs.component_property}
        response = self.client.post(
            "/_dash-update-component",
            json={
                "output": key,
                "outputs": outputs,
                "inputs": [dependency(dep) for dep in callback["inputs"]],
                "state": [dependency(dep) for dep in callback["state"]],
                "changedPropIds": [f"{identity(trigger)}.{prop}"],
            },
        )
        assert response.status_code in {200, 204}, response.get_data(as_text=True)
        if response.status_code == 204:
            return {}
        result = response.get_json()["response"]
        for component, properties in result.items():
            parsed = json.loads(component) if component.startswith("{") else component
            self.props(parsed).update(properties)
        return result

    def refresh(self) -> None:
        revision = self.get("revision", "data")
        for prefix in ["transaction", "analysis"]:
            self.call(f"{prefix}-filters.data", "revision", "data", revision)
        self.call("categories-table.rowData", "revision", "data", revision)
        self.call('"index":"main"', "revision", "data", revision)
        for prefix in ["overview", "analysis"]:
            self.call(f"{prefix}-currency.options", "revision", "data", revision)
            self.call(f"{prefix}-report.children", "revision", "data", revision)
        self.call("analysis-period.options", "revision", "data", revision)

    def select(self, row: dict[str, Any], index: str = "main") -> None:
        self.call(
            "selected-transaction.data",
            {"type": "transactions", "index": index},
            "cellClicked",
            {"rowId": row["id"], "colId": "cleaned_description"},
        )
        self.call(
            "transaction-description.children",
            "selected-transaction",
            "data",
            self.get("selected-transaction", "data"),
        )
