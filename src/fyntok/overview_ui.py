"""A compact, unfiltered snapshot of one booking month."""

from dash import Dash, html
from sqlalchemy.engine import Engine

from .analysis_ui import register_report, report_layout


def build_overview() -> html.Section:
    return report_layout("overview", overview=True)


def register_overview(app: Dash, engine: Engine) -> None:
    register_report(app, engine, "overview", overview=True)
