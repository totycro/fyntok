"""Dash controls for authentication, reviewed suggestions, and durable AI runs."""

import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from typing import Any
from uuid import uuid4

from dash import Dash, Input, Output, State, ctx, dcc, html, no_update
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .ai import AI_MODEL, AIClient, AIRequestError
from .analysis_ui import read_filters
from .auth import ChatGPTSession
from .categorization import (
    approve_proposal,
    cancel_run,
    categorization_count,
    execute_run,
    propose_categories,
    resume_run,
    run_progress,
    start_run,
)
from .dash_ui import callback, columns, dropdown, grid
from .models import CategorizationRun
from .queries import Filters

logger = logging.getLogger(__name__)


class AIWork:
    """One local worker owns account changes and AI actions across browser sessions."""

    def __init__(self, engine: Engine, client: AIClient, auth: ChatGPTSession) -> None:
        self.engine = engine
        self.client = client
        self.auth = auth
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fyntok-ui")
        self.lock = Lock()
        self.future: Future[Any] | None = None
        self.job_id: str | None = None
        self.kind = ""

    @property
    def busy(self) -> bool:
        return self.future is not None and not self.future.done()

    def submit(self, kind: str, account: str | None, action: Callable[[], Any]) -> str:
        with self.lock:
            if self.busy:
                raise ValueError("Wait for the current AI work to finish.")
            self.job_id = str(uuid4())
            self.kind = kind

            def work() -> Any:
                try:
                    if bool(account):
                        self.auth.select(account)
                    return action()
                except Exception:
                    logger.exception("AI action %s failed", kind)
                    raise

            self.future = self.pool.submit(work)
            return self.job_id

    def start(
        self, account: str | None, filters: Filters, *, only_uncategorized: bool
    ) -> tuple[str, str]:
        # Reserve the worker before creating a durable run to avoid orphan pending runs.
        with self.lock:
            if self.busy:
                raise ValueError("Wait for the current AI work to finish.")
            if bool(account):
                self.auth.select(account)
            run_id = start_run(
                self.engine, AI_MODEL, filters, only_uncategorized=only_uncategorized
            )
            self.job_id, self.kind = str(uuid4()), "run"

            def work() -> Any:
                try:
                    return execute_run(self.engine, self.client, run_id)
                except Exception:
                    logger.exception("Categorization worker failed")
                    raise

            self.future = self.pool.submit(work)
            return self.job_id, run_id

    def close(self) -> None:
        self.pool.shutdown(wait=True)


def history_options(engine: Engine) -> list[dict[str, str]]:
    with Session(engine) as session:
        return [
            {"label": f"{run.created_at} · {run.model} · {run.status}", "value": run.id}
            for run in session.scalars(
                select(CategorizationRun).order_by(CategorizationRun.created_at.desc())
            )
        ]


def build_ai_panel(engine: Engine, auth: ChatGPTSession) -> html.Div:
    history = history_options(engine)
    return html.Div(
        [
            html.H2("AI categorization"),
            html.P(
                "AI receives cleaned descriptions, category definitions, and relevant confirmed "
                "examples. Review definitions before sending. "
                "Individual corrections remain protected."
            ),
            html.Div(id="ai-connection", role="status"),
            html.Details(
                [
                    html.Summary("ChatGPT account"),
                    dropdown(
                        "ai-account", "ChatGPT account", auth.accounts(), auth.active_account()
                    ),
                    html.Div(
                        [
                            html.Button("Continue with ChatGPT", id="ai-login"),
                            html.Button("Sign out", id="ai-signout"),
                        ],
                        className="actions",
                    ),
                    html.A(
                        "Open ChatGPT sign-in",
                        id="ai-login-link",
                        href="",
                        target="_blank",
                        rel="noopener noreferrer",
                        style={"display": "none"},
                    ),
                    html.P(
                        html.A(
                            "ChatGPT usage and app access",
                            href="https://chatgpt.com/settings/usage",
                            target="_blank",
                            rel="noopener noreferrer",
                        )
                    ),
                    html.P(f"AI model: {AI_MODEL}"),
                ]
            ),
            html.Div(id="ai-status", role="status", className="status"),
            html.Div(
                [
                    html.Button("Propose categories", id="ai-propose"),
                    html.Button("Recalculate categories — all transactions", id="ai-run-all"),
                    html.Button(
                        "Recalculate categories — Transactions filters", id="ai-run-filtered"
                    ),
                ],
                className="actions",
            ),
            dcc.Checklist(
                id="ai-uncategorized",
                options=[{"label": "Only uncategorized", "value": "only"}],
                value=[],
            ),
            html.Div(id="ai-count"),
            html.H3("Review suggestions"),
            html.P(
                "Edit a suggestion, select its row, then approve or dismiss it. "
                "Categories are added only after approval."
            ),
            grid("ai-proposals", [], columns("name", "definition", "kind"), editable=True),
            html.Div(
                [
                    html.Button("Approve category", id="ai-approve"),
                    html.Button("Dismiss", id="ai-dismiss"),
                ],
                className="actions",
            ),
            html.H3("Run history and recovery"),
            dropdown(
                "ai-run", "Categorization run", history, history[0]["value"] if history else None
            ),
            html.Div(id="ai-progress", role="status", className="status"),
            html.Div(
                [
                    html.Button("Cancel selected run", id="ai-cancel"),
                    html.Button("Resume / recover selected run", id="ai-resume"),
                ],
                className="actions",
            ),
            dcc.Store(id="ai-task"),
            dcc.Store(id="ai-polled", data={}),
            dcc.Interval(id="ai-poll", interval=1000),
        ]
    )


def register_ai(app: Dash, work: AIWork) -> None:
    engine, auth = work.engine, work.auth
    actions = [
        "login",
        "signout",
        "propose",
        "run-all",
        "run-filtered",
        "cancel",
        "resume",
        "approve",
        "dismiss",
    ]

    @callback(
        app,
        Output("ai-status", "children", allow_duplicate=True),
        Output("ai-task", "data"),
        Output("ai-run", "value"),
        Output("ai-proposals", "rowData", allow_duplicate=True),
        Output("revision", "data", allow_duplicate=True),
        *[Input(f"ai-{action}", "n_clicks") for action in actions],
        State("ai-account", "value"),
        State("ai-run", "value"),
        State("transaction-filters", "data"),
        State("ai-uncategorized", "value"),
        State("ai-proposals", "rowData"),
        State("ai-proposals", "selectedRows"),
        prevent_initial_call=True,
    )
    def command(*values: Any) -> tuple[Any, ...]:
        account, run_id, data, only, proposals, selected = values[len(actions) :]
        action = str(ctx.triggered_id).removeprefix("ai-")
        logger.debug("AI command: %s", action)
        try:
            if action in {"approve", "dismiss"}:
                if not selected:
                    raise ValueError("Select a suggestion first.")
                suggestion = next(row for row in proposals if row["id"] == selected[0]["id"])
                if action == "approve":
                    approve_proposal(
                        engine, {key: suggestion[key] for key in ("name", "definition", "kind")}
                    )
                remaining = [row for row in proposals if row["id"] != suggestion["id"]]
                return (
                    "Category approved and saved."
                    if action == "approve"
                    else "Suggestion dismissed.",
                    no_update,
                    no_update,
                    remaining,
                    str(uuid4()),
                )
            if action == "cancel":
                if not bool(run_id):
                    raise ValueError("Select a run first.")
                cancel_run(engine, run_id)
                return "Cancellation saved.", no_update, no_update, no_update, str(uuid4())
            if action in {"run-all", "run-filtered"}:
                filters = read_filters(data) if action == "run-filtered" else Filters()
                job_id, run_id = work.start(account, filters, only_uncategorized=bool(only))
                return "Categorization started.", job_id, run_id, no_update, no_update
            if action == "resume":
                if not bool(run_id):
                    raise ValueError("Select a run first.")
                job = work.submit("run", account, lambda: resume_run(engine, work.client, run_id))
            elif action == "propose":
                job = work.submit(
                    "proposals", account, lambda: propose_categories(engine, work.client, AI_MODEL)
                )
            elif action == "login":
                job = work.submit("login", None, lambda: auth.start_login(account))
            else:
                job = work.submit("signout", account, auth.sign_out)
            return "Working…", job, no_update, no_update, no_update
        except (ValueError, SQLAlchemyError, StopIteration) as exc:
            logger.exception("AI command %s rejected", action)
            return str(exc), no_update, no_update, no_update, no_update

    @callback(
        app,
        Output("ai-status", "children", allow_duplicate=True),
        Output("ai-proposals", "rowData", allow_duplicate=True),
        Output("ai-login-link", "href"),
        Output("ai-login-link", "style"),
        Output("ai-polled", "data"),
        Output("ai-progress", "children"),
        Output("ai-run", "options"),
        Output("ai-account", "options"),
        Output("ai-connection", "children"),
        Output("revision", "data", allow_duplicate=True),
        *[
            Output(f"ai-{action}", "disabled")
            for action in ["propose", "run-all", "run-filtered", "resume", "signout", "login"]
        ],
        Input("ai-poll", "n_intervals"),
        Input("ai-run", "value"),
        State("ai-task", "data"),
        State("ai-polled", "data"),
        State("ai-account", "value"),
        prevent_initial_call=True,
    )
    def poll(
        _tick: int,
        run_id: str | None,
        task: str | None,
        previous: dict[str, Any],
        account: str | None,
    ) -> tuple[Any, ...]:
        seen = dict(previous or {})
        message: Any = no_update
        proposals: Any = no_update
        url: Any = no_update
        style: Any = no_update
        revision: Any = no_update
        with work.lock:
            current_job, kind, future = work.job_id, work.kind, work.future
        if (
            bool(task)
            and task == current_job
            and task != seen.get("task")
            and future is not None
            and future.done()
        ):
            seen["task"] = task
            try:
                result = future.result()
                match kind:
                    case "proposals":
                        proposals = [dict(row, id=str(uuid4())) for row in result]
                        message = f"{len(proposals)} suggestions ready for review."
                    case "login":
                        url, style = result, {"display": "block"}
                        message = "Open ChatGPT sign-in, then return here to use AI."
                    case "signout":
                        message = (
                            "Signed out."
                            if result is True
                            else "Signed out locally. Remote revocation was not confirmed; "
                            "disconnect fyntok in ChatGPT Settings."
                        )
                        url, style = "", {"display": "none"}
                    case _:
                        message = "Categorization worker finished. See saved run progress."
                revision = str(uuid4())
            except Exception as exc:
                message = (
                    f"AI action unavailable ({type(exc).__name__}). Manual review "
                    "remains available." + (f" {exc}" if isinstance(exc, AIRequestError) else "")
                )
        progress: Any = "Choose a saved run to inspect progress."
        if bool(run_id):
            try:
                saved = run_progress(engine, run_id)
                progress = (
                    f"{saved['status']} · {saved['done']:,}/{saved['total']:,} processed · "
                    f"{saved['applied']:,} applied · {saved['skipped']:,} skipped"
                    + (f" · {saved['error']}" if saved["error"] else "")
                )
                if saved != seen.get("progress"):
                    seen["progress"] = saved
                    revision = str(uuid4())
            except ValueError as exc:
                progress = str(exc)
        active = account or auth.active_account()
        connected = auth.is_connected(active)
        connection = (
            "Connected to ChatGPT. Ready to use AI."
            if connected
            else "Sign in with ChatGPT to use AI. Manual review is always available."
        )
        if bool(auth.login_error):
            connection += f" Sign-in unavailable: {auth.login_error}"
        if connected and auth.pending is None:
            url, style = "", {"display": "none"}
        return (
            message,
            proposals,
            url,
            style,
            seen,
            progress,
            history_options(engine),
            auth.accounts(),
            connection,
            revision,
            *([work.busy] * 6),
        )

    @callback(
        app,
        Output("ai-count", "children"),
        Input("revision", "data"),
        Input("transaction-filters", "data"),
        Input("ai-uncategorized", "value"),
    )
    def count(_revision: str, data: dict[str, Any] | None, only: list[str]) -> str:
        return (
            f"Eligible: {categorization_count(engine, only_uncategorized=bool(only)):,} "
            "in all transactions; "
            f"{categorization_count(engine, read_filters(data), only_uncategorized=bool(only)):,} "
            "in applied Transactions filters. Manual assignments remain protected."
        )
