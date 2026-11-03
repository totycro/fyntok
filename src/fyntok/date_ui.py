"""Date controls use ISO dates at the Dash callback boundary."""

from dash import dcc, html


def iso_date_input(identity: str, label: str) -> html.Div:
    return html.Div(
        [
            html.Label(label, htmlFor=identity),
            dcc.DatePickerSingle(
                id=identity, display_format="YYYY-MM-DD", clearable=True, first_day_of_week=1
            ),
        ],
        className="control",
    )
