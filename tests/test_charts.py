"""Heatmap colors preserve spending comparisons without outlier washout."""

from math import isfinite

import pytest

from fyntok.charts import spending_heatmap


def test_heatmap_compresses_outliers_and_preserves_amounts_and_drilldown() -> None:
    months = ["2024-01", "2024-02", "2024-03"]
    rows = [
        {
            "key": "food",
            "name": "Food",
            "monthly_cents": dict(zip(months, [10000, 100000, 100000000], strict=True)),
        },
        {
            "key": "travel",
            "name": "Travel",
            "monthly_cents": dict(zip(months, [10000, 0, None], strict=True)),
        },
    ]
    trace = spending_heatmap(rows, months, "EUR").data[0]
    small, medium, large = trace.z[0]
    assert 0 < small < medium < large
    assert small / large > 0.25
    assert trace.z[1] == [small, 0, None]
    assert trace.zmin == 0
    assert trace.zmax == large
    assert trace.customdata[0][2] == {
        "category_id": "food",
        "start": "2024-03-01",
        "month": "2024-03",
        "amount": 1000000,
    }
    assert trace.customdata[1][1]["amount"] == 0
    assert trace.customdata[1][2]["amount"] is None
    assert "%{customdata.amount:,.2f} EUR" in trace.hovertemplate
    assert not trace.hoverongaps
    assert trace.colorbar.title.text == "EUR · log scale"
    assert trace.colorbar.ticktext[0] == "0.00"
    assert trace.colorbar.ticktext[-1] == "1,000,000.00"
    for position, label in zip(trace.colorbar.tickvals, trace.colorbar.ticktext, strict=True):
        assert 10**position - 1 == pytest.approx(float(label.replace(",", "")))


@pytest.mark.parametrize("cents", [0, None, 1, 50, 100, 12345])
def test_heatmap_handles_uniform_zero_missing_and_small_values(cents: int | None) -> None:
    months = ["2024-01", "2024-02"]
    rows = [{"key": "food", "name": "Food", "monthly_cents": dict.fromkeys(months, cents)}]
    trace = spending_heatmap(rows, months, "USD").data[0]
    assert trace.z[0][0] == trace.z[0][1]
    assert isfinite(trace.zmax)
    assert trace.zmax > trace.zmin
    assert all(isfinite(value) for value in trace.colorbar.tickvals)
    if cents is None:
        assert trace.z[0] == [None, None]
    elif cents == 0:
        assert trace.z[0] == [0, 0]
    else:
        assert trace.z[0][0] == trace.zmax
        assert trace.customdata[0][0]["amount"] == cents / 100


def test_heatmap_handles_empty_rows_and_months() -> None:
    assert not spending_heatmap([], [], "EUR").data
    figure = spending_heatmap([{"key": "food", "name": "Food", "monthly_cents": {}}], [], "EUR")
    assert figure.data[0].z == ([],)
