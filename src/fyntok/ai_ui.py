"""Browser actions for ChatGPT access, proposal review, and historical recalculation."""
import logging

from nicegui import run, ui
from sqlalchemy import select
from sqlalchemy.orm import Session

from .categorization import (approve_proposal, cancel_run, execute_run, propose_categories, resume_run,
                             run_progress, start_run)
from .models import CategorizationRun
from .queries import Filters


logger = logging.getLogger(__name__)


def build_ai_panel(engine, client, auth, filters, refresh):
    state = {'busy': False, 'running': False, 'run_id': None, 'last_progress': None}
    with ui.expansion('AI categorization', icon='auto_awesome', value=True).classes('w-full'):
        ui.label('Use your ChatGPT plan to propose categories and recalculate history. Individual corrections remain protected.')
        ui.label('AI receives minimized merchant descriptions, category definitions, and relevant confirmed examples. Review definitions before sending.')
        ui.link('ChatGPT usage and app access', 'https://chatgpt.com/settings/usage', new_tab=True)
        account = ui.select(auth.accounts(), label='ChatGPT account', value=auth.active_account(), clearable=True,
                            on_change=lambda: logger.debug('Select ChatGPT account: selection changed')).classes('w-96')
        authorization = ui.link('Open ChatGPT sign-in', '#', new_tab=True)
        authorization.set_visibility(False)
        status = ui.label('Load models after signing in. Manual review is always available.')
        model = ui.select({}, label='AI model',
                          on_change=lambda: logger.debug('Select AI model: selection changed')).classes('w-96')

        async def login():
            logger.debug('Continue with ChatGPT: starting sign-in')
            try:
                url = await run.io_bound(auth.start_login, account.value)
                authorization.props['href'] = url
                authorization.update()
                authorization.set_visibility(True)
                status.set_text('Open ChatGPT sign-in, then return here and load models.')
                logger.debug('Continue with ChatGPT: sign-in link ready')
            except Exception as exc:
                logger.warning('Continue with ChatGPT failed (%s)', type(exc).__name__)
                status.set_text(f'Sign-in unavailable ({type(exc).__name__}). Try again.')

        async def load_models():
            logger.debug('Load models: fetching available models')
            try:
                if account.value:
                    await run.io_bound(auth.select, account.value)
                catalog = await run.io_bound(client.models)
                account.options = auth.accounts()
                account.value = auth.active_account()
                account.update()
                model.options = {m['slug']: m['display_name'] for m in catalog}
                model.value = 'gpt-6-luna' if 'gpt-6-luna' in model.options else next(iter(model.options), None)
                model.update()
                authorization.set_visibility(False)
                status.set_text('ChatGPT plan connected.' if catalog else 'No available models for this account.')
                logger.debug('Load models: completed with %d available models', len(catalog))
            except Exception as exc:
                logger.warning('Load models failed (%s)', type(exc).__name__)
                model.options, model.value = {}, None
                model.update()
                status.set_text(f'AI unavailable ({type(exc).__name__}). Sign in with plan permission or check plan limits.')

        async def sign_out():
            logger.debug('Sign out: requested')
            if state['busy']:
                logger.debug('Sign out: blocked because AI work is in progress')
                ui.notify('Wait for the current AI work to finish before signing out.', type='warning')
                return
            try:
                confirmed = await run.io_bound(auth.sign_out)
            except Exception as exc:
                logger.warning('Sign out failed (%s)', type(exc).__name__)
                raise
            model.options, model.value = {}, None
            model.update()
            authorization.set_visibility(False)
            status.set_text('Signed out.' if confirmed else 'Signed out locally. Remote revocation was not confirmed; disconnect fyntok in ChatGPT Settings.')
            logger.debug('Sign out: completed; remote revocation confirmed=%s', confirmed)

        with ui.row():
            ui.button('Continue with ChatGPT', on_click=login)
            ui.button('Load models', on_click=load_models).props('outline')
            ui.button('Sign out', on_click=sign_out).props('flat')
        proposals = ui.column().classes('w-full')

        async def propose():
            logger.debug('Propose categories: requested')
            if state['busy'] or not model.value:
                logger.debug('Propose categories: blocked; busy=%s, model selected=%s', state['busy'], bool(model.value))
                ui.notify('Choose a model and wait for current AI work to finish.', type='warning')
                return
            state['busy'] = True
            status.set_text('Preparing category suggestions…')
            try:
                logger.debug('Propose categories: preparing descriptions and requesting AI suggestions')
                suggestions = await run.io_bound(propose_categories, engine, client, model.value)
                proposals.clear()
                with proposals:
                    ui.label('Review suggestions. Each category is added only when you approve it.')
                    for suggestion in suggestions:
                        with ui.card().classes('w-full') as card:
                            name = ui.input('Suggested name', value=suggestion['name'])
                            definition = ui.textarea('Suggested definition', value=suggestion['definition']).classes('w-full')
                            kind = ui.select(['spending', 'income', 'transfer'], label='Suggested financial type', value=suggestion['kind'])

                            def approve(name=name, definition=definition, kind=kind, card=card):
                                logger.debug('Approve category: saving reviewed suggestion')
                                try:
                                    approve_proposal(engine, dict(name=name.value, definition=definition.value, kind=kind.value))
                                    card.delete()
                                    refresh()
                                    ui.notify('Category approved and saved.', type='positive')
                                    logger.debug('Approve category: completed')
                                except ValueError as exc:
                                    logger.warning('Approve category failed (%s)', type(exc).__name__)
                                    ui.notify(str(exc), type='negative')
                            ui.button('Approve category', on_click=approve)
                            def dismiss(card=card):
                                logger.debug('Dismiss category suggestion: removing suggestion from review')
                                card.delete()
                            ui.button('Dismiss', on_click=dismiss).props('flat')
                status.set_text(f'{len(suggestions)} suggestions ready for review.')
                logger.debug('Propose categories: completed with %d suggestions', len(suggestions))
            except Exception as exc:
                logger.warning('Propose categories failed (%s)', type(exc).__name__)
                status.set_text(f'Category suggestions unavailable ({type(exc).__name__}). Manual category creation remains available.')
            finally:
                state['busy'] = False

        def history_options():
            with Session(engine) as session:
                return {r.id: f'{r.created_at} — {r.model} — {r.status}' for r in
                        session.scalars(select(CategorizationRun).order_by(CategorizationRun.created_at.desc()))}

        history = ui.select(history_options(), label='Categorization run',
                            on_change=lambda e: select_run(e.value)).classes('w-full')

        def select_run(value):
            logger.debug('Select categorization run: requested')
            if state['running']:
                logger.debug('Select categorization run: blocked because a run is in progress')
                history.value = state['run_id']
                history.update()
                return
            state['run_id'] = value
            poll()

        def poll(force=True):
            if state['run_id']:
                p = run_progress(engine, state['run_id'])
                if not force and p == state['last_progress']:
                    return
                if p != state['last_progress']:
                    logger.debug('Categorization run progress: status=%s, processed=%d/%d, applied=%d, skipped=%d',
                                 p['status'], p['done'], p['total'], p['applied'], p['skipped'])
                state['last_progress'] = p
                history.options = history_options()
                history.value = state['run_id']
                history.update()
                progress.set_value(p['done'] / p['total'] if p['total'] else 1)
                status.set_text(f"{p['status'].title()}: {p['applied']} applied, {p['skipped']} protected or changed; {p['done']} / {p['total']} processed." + (' ' + p['error'] if p['error'] else ''))

        def cancel():
            logger.debug('Cancel selected run: requested')
            if state['run_id']:
                cancel_run(engine, state['run_id'])
                poll()
                ui.notify('Cancellation saved. The current request may finish, but its result will be discarded.')
                logger.debug('Cancel selected run: cancellation saved')
            else:
                logger.debug('Cancel selected run: blocked because no run is selected')

        async def resume():
            logger.debug('Resume / recover selected run: requested')
            if state['busy'] or not state['run_id']:
                logger.debug('Resume / recover selected run: blocked; busy=%s, run selected=%s',
                             state['busy'], bool(state['run_id']))
                ui.notify('Select an unfinished run and wait for current AI work to finish.', type='warning')
                return
            state['busy'] = state['running'] = True
            try:
                logger.debug('Resume / recover selected run: recovering unfinished work')
                result = await run.io_bound(resume_run, engine, client, state['run_id'])
                logger.debug('Resume / recover selected run: worker finished with status=%s', result['status'])
            except ValueError as exc:
                logger.warning('Resume / recover selected run failed (%s)', type(exc).__name__)
                ui.notify(str(exc), type='warning')
            finally:
                state['busy'] = state['running'] = False
                poll()
                refresh()

        async def recalculate(filtered=False):
            action = 'Recalculate categories — current filters' if filtered else 'Recalculate categories — all transactions'
            logger.debug('%s: requested', action)
            if state['busy'] or not model.value:
                logger.debug('%s: blocked; busy=%s, model selected=%s', action, state['busy'], bool(model.value))
                ui.notify('Choose a model and wait for current AI work to finish.', type='warning')
                return
            try:
                scope = filters() if filtered else Filters()
                state['run_id'] = start_run(engine, model.value, scope)
                logger.debug('%s: saved run; starting background categorization', action)
            except ValueError as exc:
                logger.warning('%s failed to start (%s)', action, type(exc).__name__)
                ui.notify(str(exc), type='negative')
                return
            state['busy'] = True
            state['running'] = True
            poll()
            try:
                result = await run.io_bound(execute_run, engine, client, state['run_id'])
                logger.debug('%s: worker finished with status=%s', action, result['status'])
            except ValueError as exc:
                logger.warning('%s failed (%s)', action, type(exc).__name__)
                ui.notify(str(exc), type='warning')
            finally:
                state['busy'] = False
                state['running'] = False
                poll()
                refresh()

        with ui.row():
            ui.button('Propose categories', on_click=propose)
            ui.button('Recalculate categories — all transactions', on_click=lambda: recalculate(False))
            ui.button('Recalculate categories — current filters', on_click=lambda: recalculate(True)).props('outline')
        with ui.row():
            ui.button('Cancel selected run', on_click=cancel).props('outline')
            ui.button('Resume / recover selected run', on_click=resume).props('outline')
        ui.label('Resume keeps the original model, selection, definitions, and examples. After edits, start a fresh recalculation. Recovery waits until any live worker has stopped.')
        progress = ui.linear_progress(value=0).classes('w-full')
        ui.timer(0.5, lambda: poll(False))
        with Session(engine) as session:
            recent = session.scalar(select(CategorizationRun).order_by(CategorizationRun.created_at.desc()).limit(1))
            if recent:
                state['run_id'] = recent.id
                poll()
