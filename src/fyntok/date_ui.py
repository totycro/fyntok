"""Date controls with a locale-independent ISO display."""

from nicegui import ui
from nicegui.events import Handler, ValueChangeEventArguments


def iso_date_input(
    label: str,
    *,
    value: str | None = "",
    on_change: Handler[ValueChangeEventArguments[str | None]] | None = None,
) -> ui.input:
    with (
        ui.input(label, value=value, placeholder="YYYY-MM-DD", on_change=on_change).props(
            "mask=####-##-##"
        ) as control,
        control.add_slot("append"),
    ):
        with ui.menu().props("no-parent-event") as calendar:
            ui.date(mask="YYYY-MM-DD").bind_value(control)
        ui.button(icon="event", on_click=calendar.open).props("flat round dense").tooltip(
            "Choose date"
        )
    return control
