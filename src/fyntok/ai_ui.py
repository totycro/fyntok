"""Browser actions for ChatGPT access, proposal review, and historical recalculation."""

import logging
from collections.abc import Callable
from typing import Any, TypedDict

from nicegui import run, ui
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .ai import AI_MODEL, AIClient, AIRequestError
from .auth import ChatGPTSession
from .categorization import (
    approve_proposal,
    cancel_run,
    execute_run,
    propose_categories,
    resume_run,
    run_progress,
    start_run,
)
from .models import CategorizationRun
from .queries import Filters

logger = logging.getLogger(__name__)


class PanelState(TypedDict):
    busy: bool
    running: bool
    run_id: str | None
    last_progress: dict[str, Any] | None


def build_ai_panel(
    engine: Engine,
    client: AIClient,
    auth: ChatGPTSession,
    filters: Callable[[], Filters],
    refresh: Callable[[], None],
    on_status: Callable[[str], None],
) -> None:
    state: PanelState = {"busy": False, "running": False, "run_id": None, "last_progress": None}
    with ui.column().classes("w-full"):
        ui.label("AI categorization").classes("text-xl font-semibold")
        ui.label(
            "Use your ChatGPT plan to propose categories and recalculate "
            "history. Individual corrections remain protected."
        )
        ui.label(
            "AI receives cleaned merchant descriptions, category "
            "definitions, and relevant confirmed examples. Review definitions "
            "before sending."
        )
        connection = ui.label()
        status = ui.label().props("role=status aria-live=polite")
        actions = ui.row().classes("w-full")
        progress = ui.linear_progress(value=0).classes("w-full")
        progress.set_visibility(False)
        proposals = ui.column().classes("w-full")
        with ui.expansion("ChatGPT account", icon="account_circle").classes(
            "w-full"
        ) as account_settings:
            ui.link(
                "ChatGPT usage and app access", "https://chatgpt.com/settings/usage", new_tab=True
            )
            account = ui.select(
                auth.accounts(),
                label="ChatGPT account",
                value=auth.active_account(),
                clearable=True,
                on_change=lambda: logger.debug("Select ChatGPT account: selection changed"),
            ).classes("w-full")
            authorization = ui.link("Open ChatGPT sign-in", "#", new_tab=True)
            authorization.set_visibility(False)
            ui.label(f"AI model: {AI_MODEL}")
        last_active_account = auth.active_account()

        def refresh_accounts() -> None:
            nonlocal last_active_account
            active = auth.active_account()
            options = auth.accounts()
            if options != account.options or active != last_active_account:
                account.options = options
                if active != last_active_account:
                    account.value = active
                account.update()
                last_active_account = active
            connected = auth.is_connected(account.value)
            connection.set_text(
                "Connected to ChatGPT. Ready to use AI."
                if connected
                else "Sign in with ChatGPT to use AI. Manual review is always available."
            )
            login_button.set_text("Reconnect" if connected else "Continue with ChatGPT")
            if connected and auth.pending is None and auth.login_error is None:
                authorization.set_visibility(False)
                if status.text == "Open ChatGPT sign-in, then return here to use AI.":
                    status.set_text("")

        async def prepare_account() -> None:
            refresh_accounts()
            if account.value:
                await run.io_bound(auth.select, account.value)
                refresh_accounts()

        async def login() -> None:
            logger.debug("Continue with ChatGPT: starting sign-in")
            try:
                url = await run.io_bound(auth.start_login, account.value)
                authorization.props["href"] = url
                authorization.update()
                authorization.set_visibility(True)
                status.set_text("Open ChatGPT sign-in, then return here to use AI.")
                logger.debug("Continue with ChatGPT: sign-in link ready")
            except Exception as exc:
                logger.exception("Continue with ChatGPT failed (%s)", type(exc).__name__)
                status.set_text(f"Sign-in unavailable ({type(exc).__name__}). Try again.")

        async def sign_out() -> None:
            logger.debug("Sign out: requested")
            if state["busy"]:
                logger.debug("Sign out: blocked because AI work is in progress")
                ui.notify(
                    "Wait for the current AI work to finish before signing out.", type="warning"
                )
                return
            try:
                await prepare_account()
                confirmed = await run.io_bound(auth.sign_out)
            except Exception as exc:
                logger.exception("Sign out failed (%s)", type(exc).__name__)
                raise
            authorization.set_visibility(False)
            refresh_accounts()
            status.set_text(
                "Signed out."
                if confirmed is True
                else (
                    "Signed out locally. Remote revocation was not confirmed; "
                    "disconnect fyntok in ChatGPT Settings."
                )
            )
            logger.debug("Sign out: completed; remote revocation confirmed=%s", confirmed)

        with account_settings, ui.row():
            login_button = ui.button("Continue with ChatGPT", on_click=login)
            ui.button("Sign out", on_click=sign_out).props("flat")
        refresh_accounts()

        def update_proposal_activity() -> None:
            remaining = sum(isinstance(child, ui.card) for child in proposals.default_slot.children)
            on_status(f"{remaining} suggestions to review" if remaining else "")

        async def propose() -> None:
            logger.debug("Propose categories: requested")
            if state["busy"]:
                logger.debug(
                    "Propose categories: blocked; busy=%s",
                    state["busy"],
                )
                ui.notify("Wait for current AI work to finish.", type="warning")
                return
            state["busy"] = True
            status.set_text("Preparing category suggestions…")
            on_status("Preparing suggestions…")
            try:
                logger.debug(
                    "Propose categories: preparing descriptions and requesting AI suggestions"
                )
                await prepare_account()
                suggestions = await run.io_bound(propose_categories, engine, client, AI_MODEL)
                if suggestions is None:
                    raise RuntimeError("Background operation was cancelled.")
                proposals.clear()
                with proposals:
                    ui.label("Review suggestions. Each category is added only when you approve it.")
                    for suggestion in suggestions:
                        with ui.card().classes("w-full") as card:
                            name = ui.input("Suggested name", value=suggestion["name"])
                            definition = ui.textarea(
                                "Suggested definition", value=suggestion["definition"]
                            ).classes("w-full")
                            kind = ui.select(
                                ["spending", "income", "transfer"],
                                label="Suggested financial type",
                                value=suggestion["kind"],
                            )

                            def approve(
                                name: ui.input = name,
                                definition: ui.textarea = definition,
                                kind: ui.select = kind,
                                card: ui.card = card,
                            ) -> None:
                                logger.debug("Approve category: saving reviewed suggestion")
                                try:
                                    approve_proposal(
                                        engine,
                                        {
                                            "name": name.value,
                                            "definition": definition.value,
                                            "kind": kind.value,
                                        },
                                    )
                                    card.delete()
                                    update_proposal_activity()
                                    refresh()
                                    ui.notify("Category approved and saved.", type="positive")
                                    logger.debug("Approve category: completed")
                                except ValueError as exc:
                                    logger.exception(
                                        "Approve category failed (%s)", type(exc).__name__
                                    )
                                    ui.notify(str(exc), type="negative")

                            ui.button("Approve category", on_click=approve)

                            def dismiss(card: ui.card = card) -> None:
                                logger.debug(
                                    "Dismiss category suggestion: removing suggestion from review"
                                )
                                card.delete()
                                update_proposal_activity()

                            ui.button("Dismiss", on_click=dismiss).props("flat")
                status.set_text(f"{len(suggestions)} suggestions ready for review.")
                update_proposal_activity()
                ui.notify(status.text, type="positive")
                logger.debug("Propose categories: completed with %d suggestions", len(suggestions))
            except Exception as exc:
                logger.exception("Propose categories failed (%s)", type(exc).__name__)
                status.set_text(
                    f"Category suggestions unavailable ({type(exc).__name__}). Manual "
                    f"category creation remains available."
                    + (f" {exc}" if isinstance(exc, AIRequestError) else "")
                )
                on_status("Suggestions failed")
                ui.notify(status.text, type="negative")
            finally:
                state["busy"] = False

        def history_options() -> dict[str, str]:
            with Session(engine) as session:
                return {
                    r.id: f"{r.created_at} — {r.model} — {r.status}"
                    for r in session.scalars(
                        select(CategorizationRun).order_by(CategorizationRun.created_at.desc())
                    )
                }

        with ui.expansion("Run history and recovery", icon="history").classes(
            "w-full"
        ) as run_settings:
            history = ui.select(
                history_options(),
                label="Categorization run",
                on_change=lambda e: select_run(e.value),
            ).classes("w-full")

        def select_run(value: str | None) -> None:
            logger.debug("Select categorization run: requested")
            if state["running"]:
                logger.debug("Select categorization run: blocked because a run is in progress")
                history.value = state["run_id"]
                history.update()
                return
            state["run_id"] = value
            poll()

        def poll(force: bool = True) -> None:
            if state["run_id"] is not None and state["run_id"] != "":
                p = run_progress(engine, state["run_id"])
                if not force and p == state["last_progress"]:
                    return
                if p != state["last_progress"]:
                    logger.debug(
                        (
                            "Categorization run progress: status=%s, processed=%d/%d, "
                            "applied=%d, skipped=%d"
                        ),
                        p["status"],
                        p["done"],
                        p["total"],
                        p["applied"],
                        p["skipped"],
                    )
                previous = state["last_progress"]
                state["last_progress"] = p
                history.options = history_options()
                history.value = state["run_id"]
                history.update()
                progress.set_visibility(True)
                progress.set_value(p["done"] / p["total"] if p["total"] else 1)
                on_status(f"{p['status'].title()}: {p['done']} / {p['total']}")
                status.set_text(
                    (
                        f"{p['status'].title()}: {p['applied']} applied, {p['skipped']} "
                        f"protected or changed; {p['done']} / {p['total']} processed."
                    )
                    + (" " + p["error"] if p["error"] else "")
                )

                if (
                    p != previous
                    and p["status"] in {"completed", "failed", "cancelled"}
                    and (
                        state["running"]
                        or (
                            previous is not None
                            and previous["id"] == p["id"]
                            and previous["status"] in {"pending", "running"}
                        )
                    )
                ):
                    ui.notify(status.text, type="negative" if p["status"] == "failed" else "info")

        def cancel() -> None:
            logger.debug("Cancel selected run: requested")
            if state["run_id"] is not None and state["run_id"] != "":
                cancel_run(engine, state["run_id"])
                poll()
                ui.notify(
                    "Cancellation saved. The current request may finish, but its "
                    "result will be discarded."
                )
                logger.debug("Cancel selected run: cancellation saved")
            else:
                logger.debug("Cancel selected run: blocked because no run is selected")

        async def resume() -> None:
            logger.debug("Resume / recover selected run: requested")
            if state["busy"] or not (state["run_id"] is not None and state["run_id"] != ""):
                logger.debug(
                    "Resume / recover selected run: blocked; busy=%s, run selected=%s",
                    state["busy"],
                    bool(state["run_id"]),
                )
                ui.notify(
                    "Select an unfinished run and wait for current AI work to finish.",
                    type="warning",
                )
                return
            state["busy"] = state["running"] = True
            try:
                logger.debug("Resume / recover selected run: recovering unfinished work")
                await prepare_account()
                result = await run.io_bound(resume_run, engine, client, state["run_id"])
                if result is None:
                    raise RuntimeError("Background operation was cancelled.")
                logger.debug(
                    "Resume / recover selected run: worker finished with status=%s",
                    result["status"],
                )
            except ValueError as exc:
                logger.exception("Resume / recover selected run failed (%s)", type(exc).__name__)
                ui.notify(str(exc), type="warning")
            finally:
                state["busy"] = state["running"] = False
                poll()
                refresh()

        async def recalculate(filtered: bool = False) -> None:
            action = (
                "Recalculate categories — current filters"
                if filtered
                else "Recalculate categories — all transactions"
            )
            logger.debug("%s: requested", action)
            if state["busy"]:
                logger.debug(
                    "%s: blocked; busy=%s",
                    action,
                    state["busy"],
                )
                ui.notify("Wait for current AI work to finish.", type="warning")
                return
            state["busy"] = True
            try:
                await prepare_account()
                scope = filters() if filtered else Filters()
                state["run_id"] = start_run(engine, AI_MODEL, scope)
                logger.debug("%s: saved run; starting background categorization", action)
                state["running"] = True
                poll()
                result = await run.io_bound(execute_run, engine, client, state["run_id"])
                if result is None:
                    raise RuntimeError("Background operation was cancelled.")
                logger.debug("%s: worker finished with status=%s", action, result["status"])
            except ValueError as exc:
                logger.exception("%s failed (%s)", action, type(exc).__name__)
                ui.notify(str(exc), type="warning")
            finally:
                state["busy"] = False
                state["running"] = False
                poll()
                refresh()

        with actions:
            ui.button("Propose categories", on_click=propose)
            ui.button(
                "Recalculate categories — all transactions", on_click=lambda: recalculate(False)
            )
            ui.button(
                "Recalculate categories — current filters", on_click=lambda: recalculate(True)
            ).props("outline")
        with run_settings, ui.row():
            ui.button("Cancel selected run", on_click=cancel).props("outline")
            ui.button("Resume / recover selected run", on_click=resume).props("outline")
        with run_settings:
            ui.label(
                "Resume keeps the original model, selection, definitions, and "
                "examples. After edits, start a fresh recalculation. Recovery "
                "waits until any live worker has stopped."
            )
        ui.timer(0.5, refresh_accounts)
        ui.timer(0.5, lambda: poll(False))
        with Session(engine) as session:
            recent = session.scalar(
                select(CategorizationRun).order_by(CategorizationRun.created_at.desc()).limit(1)
            )
            if recent:
                state["run_id"] = recent.id
                poll()
