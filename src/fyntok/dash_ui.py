"""Shared Dash controls and grids; monetary cells retain integer cents for sorting."""

from collections.abc import Callable
from datetime import date, datetime
from typing import Any, cast

import dash_ag_grid as dag
from dash import Dash, dcc, html

MONEY_FORMAT = "fyntokMoney(params.value)"


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100:,}.{abs(cents) % 100:02d}"


def json_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            key: value.isoformat() if isinstance(value, date | datetime) else value
            for key, value in row.items()
        }
        for row in rows
    ]


def columns(*names: str, money_columns: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    result = []
    for name in names:
        field = "cleaned_description" if name == "description" else name
        column: dict[str, Any] = {"field": field, "headerName": name.replace("_", " ").title()}
        if name in money_columns:
            column.update(
                field=f"{name}_cents",
                type="numericColumn",
                valueFormatter={"function": MONEY_FORMAT},
            )
        result.append(column)
    return result


def grid(
    identity: str | dict[str, str],
    rows: list[dict[str, Any]],
    fields: list[dict[str, Any]],
    *,
    key: str = "id",
    editable: bool = False,
    selectable: bool = False,
    clickable: bool = False,
) -> dag.AgGrid:
    return dag.AgGrid(
        id=identity,
        rowData=json_rows(rows),
        columnDefs=fields,
        getRowId=f"params.data.{key}",
        rowClass="clickable-row" if clickable else None,
        defaultColDef={
            "sortable": True,
            "filter": True,
            "resizable": True,
            "flex": 1,
            "minWidth": 140,
            "editable": editable,
        },
        dashGridOptions={
            "pagination": True,
            "paginationPageSize": 20,
            **({"rowSelection": {"mode": "singleRow"}} if selectable else {}),
            "stopEditingWhenCellsLoseFocus": True,
        },
        style={"height": "430px", "width": "100%"},
    )


def dropdown(
    identity: str,
    label: str,
    options: Any,
    value: Any = None,
    *,
    multi: bool = False,
    clearable: bool = True,
) -> html.Div:
    return html.Div(
        [
            html.Label(label, htmlFor=identity),
            dcc.Dropdown(
                id=identity, options=options, value=value, multi=multi, clearable=clearable
            ),
        ],
        className="control",
    )


def text_input(identity: str, label: str, value: str = "") -> html.Div:
    return html.Div(
        [
            html.Label(label, htmlFor=identity),
            dcc.Input(id=identity, value=value, type="text", debounce=True),
        ],
        className="control",
    )


def transaction_grid(identity: str, rows: list[dict[str, Any]]) -> dag.AgGrid:
    return grid(
        {"type": "transactions", "index": identity},
        rows,
        columns(
            "booking_date",
            "value_date",
            "description",
            "account_label",
            "category",
            "amount",
            "currency",
            "source",
            "repayment_status",
            money_columns=("amount",),
        ),
        clickable=True,
    )


def callback[**P, R](
    app: Dash, *dependencies: Any, **options: Any
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Dash preserves callback functions but its public decorator is typed as Any."""
    return cast(
        "Callable[[Callable[P, R]], Callable[P, R]]", app.callback(*dependencies, **options)
    )
