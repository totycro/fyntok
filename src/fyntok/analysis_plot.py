"""NiceGUI Plotly with safe resizing while report panels are hidden or replaced."""

import inspect
from pathlib import Path

from nicegui import ui


class AnalysisPlot(
    ui.plotly,
    component="analysis_plot.js",
    esm={"fyntok-plotly-base": str(Path(inspect.getfile(ui.plotly)).parent)},
):
    """Retain NiceGUI chart behavior, guarding its unconditional resize observer."""
