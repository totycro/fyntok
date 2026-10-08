"""Local NiceGUI application; all manual workflows work without AI access."""
from datetime import date
import logging

from nicegui import run, ui
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .analysis import aggregate
from .charts import monthly_figure, trend_figure
from .importer import import_content
from .manual import (assignment_history, categories, correct_transaction, remove_category,
                     save_category, undo_assignment)
from .models import Import, Transaction
from .queries import Filters, transaction_rows


logger = logging.getLogger(__name__)


def money(cents: int) -> str:
    sign = '-' if cents < 0 else ''
    return f'{sign}{abs(cents) // 100:,}.{abs(cents) % 100:02d}'


def columns(*names):
    return [dict(name=n, label=n.replace('_', ' ').title(), field=n, sortable=True, align='left') for n in names]


def build_page(engine, ai_client=None, auth=None):
    ui.label('fyntok').classes('text-3xl font-bold')
    ui.label('Explore your history and refine one transaction at a time.').classes('text-gray-600')
    with ui.expansion('Import CSV', icon='upload').classes('w-full'):
        ui.label('Headerless CP1252 CSV; semicolon separators and German dates/amounts. Repeated exports reuse existing rows.')

        async def upload(event):
            logger.debug('Import CSV: reading upload and importing transactions')
            try:
                result = await run.io_bound(import_content, engine, await event.file.read(), event.file.name)
                logger.info('CSV import completed: %d new transactions, %d duplicates',
                            result.inserted_count, result.duplicate_count)
                ui.notify(f'Imported {result.inserted_count} new transactions; {result.duplicate_count} already present.', type='positive')
                refresh()
            except (ValueError, SQLAlchemyError, UnicodeError) as exc:
                logger.warning('CSV import failed (%s)', type(exc).__name__)
                ui.notify(str(exc), type='negative')
        ui.upload(on_upload=upload, auto_upload=True, label='Choose bank export').props('accept=.csv').classes('w-full')
    with ui.row().classes('items-end w-full'):
        start = ui.input('Booking date from', placeholder='YYYY-MM-DD').props('type=date')
        end = ui.input('Through', placeholder='YYYY-MM-DD').props('type=date')
        category_filter = ui.select({}, label='Category', clearable=True).classes('w-48')
        source = ui.select(['unassigned', 'manual', 'ai', 'system', 'undo'], label='Assignment source', clearable=True).classes('w-44')
        currency = ui.select([], label='Currency', clearable=True).classes('w-28')
        merchant = ui.select([], label='Merchant', clearable=True, with_input=True).classes('w-64')
        search = ui.input('Description contains').classes('w-64')
        def apply_filters():
            logger.debug('Apply filters: refreshing analysis and transactions')
            content.refresh()
        ui.button('Apply filters', on_click=apply_filters)
        def clear():
            logger.debug('Clear filters: resetting filters and refreshing results')
            for control in (start, end, category_filter, source, currency, merchant, search):
                control.value = None
            content.refresh()
        ui.button('Clear', on_click=clear).props('flat')

    def filters():
        f = Filters(start=date.fromisoformat(start.value) if start.value else None,
                    end=date.fromisoformat(end.value) if end.value else None,
                    category_id=category_filter.value, source=source.value, currency=currency.value,
                    merchant=merchant.value, search=search.value or '')
        f.validate()
        return f

    from .auth import ChatGPTSession
    from .ai import ResponsesClient
    from .ai_ui import build_ai_panel
    auth = auth or ChatGPTSession()
    ai_client = ai_client or ResponsesClient(auth)

    def refresh():
        category_filter.options = {c['id']: c['name'] for c in categories(engine)}
        if category_filter.value not in category_filter.options:
            category_filter.value = None
        category_filter.update()
        with Session(engine) as session:
            merchant.options = list(session.scalars(select(Transaction.merchant).distinct().order_by(Transaction.merchant)))
            currency.options = list(session.scalars(select(Transaction.currency).distinct().order_by(Transaction.currency)))
        merchant.update()
        currency.update()
        content.refresh()

    def perform(action, dialog, label):
        logger.debug('%s: saving changes', label)
        try:
            action()
        except (ValueError, SQLAlchemyError) as exc:
            logger.warning('%s failed (%s)', label, type(exc).__name__)
            ui.notify(str(exc), type='negative')
            return
        dialog.close()
        dialog.delete()
        logger.info('%s: completed', label)
        refresh()

    def close_dialog(dialog, label):
        logger.debug('%s: closing dialog', label)
        dialog.close()

    def category_editor(category=None):
        label = 'Edit category' if category else 'Create category'
        logger.debug('%s: opening editor', label)
        with ui.dialog() as dialog, ui.card().classes('w-full max-w-2xl'):
            ui.label('Edit category' if category else 'Create category').classes('text-xl')
            name = ui.input('Name', value=category['name'] if category else '').classes('w-full')
            definition = ui.textarea('Definition', value=category['definition'] if category else '').classes('w-full')
            kind = ui.select({'spending': 'Spending', 'income': 'Income', 'transfer': 'Internal transfer'},
                             label='Financial type', value=category['kind'] if category else 'spending')
            if category:
                ui.label('Definition changes take effect for future categorization. Use Recalculate categories for all history or filter by this category to revisit affected transactions.')
            ui.button('Save', on_click=lambda: perform(lambda: save_category(
                engine, name.value or '', definition.value or '', kind.value,
                category_id=category['id'] if category else None,
                expected_revision=category['revision'] if category else None), dialog, label))
            ui.button('Cancel', on_click=lambda: close_dialog(dialog, f'Cancel {label.lower()}')).props('flat')
        dialog.on('hide', dialog.delete)
        dialog.open()

    def category_removal(category, merging):
        label = 'Merge category' if merging else 'Delete category'
        logger.debug('%s: opening destination selector', label)
        options = {c['id']: c['name'] for c in categories(engine) if c['id'] != category['id']}
        if merging:
            options.pop('uncategorized', None)
        with ui.dialog() as dialog, ui.card().classes('w-full max-w-xl'):
            ui.label(f'{"Merge" if merging else "Delete"} {category["name"]}').classes('text-xl')
            ui.label('Current assignments and confirmed examples move to the destination. Previous assignments remain in history.')
            destination = ui.select(options, label='Destination', value=None if merging else 'uncategorized').classes('w-full')
            ui.button('Merge category' if merging else 'Delete category', on_click=lambda: perform(
                lambda: remove_category(engine, category['id'], destination.value, expected_revision=category['revision']), dialog, label))
            ui.button('Cancel', on_click=lambda: close_dialog(dialog, f'Cancel {label.lower()}')).props('flat')
        dialog.on('hide', dialog.delete)
        dialog.open()

    def transaction_editor(row):
        logger.debug('Transaction details: opening editor and assignment history')
        with ui.dialog() as dialog, ui.card().classes('w-full max-w-4xl'):
            ui.label('Transaction details').classes('text-xl')
            ui.label(row['description']).classes('whitespace-pre-wrap')
            ui.label(f'Booking {row["booking_date"]} · Value {row["value_date"]} · {money(row["amount_cents"])} {row["currency"]}')
            ui.label(f'Assignment: {row["source"]}{" (AI-generated)" if row["origin_source"] == "ai" else ""} · {"protected manual decision" if row["protected"] else "eligible for future AI recalculation"}')
            selection = ui.select({c['id']: c['name'] for c in categories(engine)}, label='Category', value=row['category_id']).classes('w-full')
            ui.label('Saving confirms only this transaction and replaces its previous confirmed example.')
            with ui.row():
                ui.button('Confirm individual category', on_click=lambda: perform(lambda: correct_transaction(
                    engine, row['id'], selection.value, expected_revision=row['revision']), dialog, 'Confirm individual category'))
                ui.button('Undo latest assignment change', on_click=lambda: perform(lambda: undo_assignment(
                    engine, row['id'], expected_revision=row['revision']), dialog, 'Undo latest assignment change')).props('outline')
                ui.button('Close', on_click=lambda: close_dialog(dialog, 'Close transaction details')).props('flat')
            ui.label('Assignment history').classes('text-lg')
            history = assignment_history(engine, row['id'])
            ui.table(columns=columns('created_at', 'category', 'source', 'protected', 'active', 'reason'),
                     rows=history, row_key='id', pagination=10).classes('w-full')
        dialog.on('hide', dialog.delete)
        dialog.open()

    selected_tab = {'value': 'Analysis'}

    def select_tab(event):
        logger.debug('Select tab: %s', event.value)
        selected_tab['value'] = event.value

    @ui.refreshable
    def content():
        try:
            f = filters()
            rows = transaction_rows(engine, f)
            monthly = aggregate(engine, filters=f)
            summaries = aggregate(engine, by='merchant', filters=f)
        except ValueError as exc:
            logger.debug('Refresh results: invalid filters (%s)', type(exc).__name__)
            ui.label(str(exc)).classes('text-red-700')
            return
        logger.debug('Refresh results: displaying %d matching transactions', len(rows))
        ui.label(f'{len(rows):,} matching transactions').classes('text-xl')
        ui.label('Spending is positive; income, internal transfers and Uncategorized are separate. Transfers and Uncategorized show signed net values. Currencies are separate series.')
        with ui.tabs(value=selected_tab['value'], on_change=select_tab).classes('w-full') as tabs:
            dashboard = ui.tab('Analysis')
            transactions = ui.tab('Transactions')
            category_tab = ui.tab('Categories')
            imports = ui.tab('Import history')
        with ui.tab_panels(tabs, value=selected_tab['value']).classes('w-full'):
            with ui.tab_panel(dashboard):
                ui.plotly(monthly_figure(monthly)).classes('w-full')
                ui.plotly(trend_figure(monthly)).classes('w-full')
                ui.label('Merchant summaries').classes('text-xl')
                display = [dict(r, outgoing=money(r['outgoing_cents']), incoming=money(r['incoming_cents']),
                                net=money(r['net_cents']), key=i) for i, r in enumerate(summaries)]
                ui.table(columns=columns('merchant', 'category_name', 'bucket', 'currency', 'transaction_count', 'outgoing', 'incoming', 'net'),
                         rows=display, row_key='key', pagination=20).classes('w-full')
            with ui.tab_panel(transactions):
                ui.label('Click a row to correct that transaction or view its history.')
                display = [dict(r, booking_date=r['booking_date'].isoformat(), value_date=r['value_date'].isoformat(),
                                amount=money(r['amount_cents']), source_label='AI-generated' if r['origin_source'] == 'ai' else r['source']) for r in rows]
                table = ui.table(columns=columns('booking_date', 'value_date', 'description', 'amount', 'currency', 'category', 'source_label', 'protected'),
                                 rows=display, row_key='id', pagination=25).classes('w-full')
                table.on('rowClick', lambda e: transaction_editor(e.args[1]), [[], None, None])
            with ui.tab_panel(category_tab):
                ui.button('Create category', on_click=lambda: category_editor())
                for c in categories(engine):
                    with ui.card().classes('w-full'):
                        ui.label(f'{c["name"]} · {c["kind"]}').classes('text-lg')
                        ui.label(c['definition']).classes('whitespace-pre-wrap')
                        if c['id'] != 'uncategorized':
                            with ui.row():
                                ui.button('Edit', on_click=lambda c=c: category_editor(c)).props('flat')
                                ui.button('Merge', on_click=lambda c=c: category_removal(c, True)).props('flat')
                                ui.button('Delete', on_click=lambda c=c: category_removal(c, False)).props('flat')
            with ui.tab_panel(imports):
                with Session(engine) as session:
                    records = [dict(id=r.id, filename=r.filename, created_at=r.created_at.isoformat(),
                                    row_count=r.row_count, inserted_count=r.inserted_count, duplicate_count=r.duplicate_count)
                               for r in session.scalars(select(Import).order_by(Import.created_at.desc()))]
                ui.table(columns=columns('filename', 'created_at', 'row_count', 'inserted_count', 'duplicate_count'),
                         rows=records, row_key='id', pagination=20).classes('w-full')
    build_ai_panel(engine, ai_client, auth, filters, refresh)
    content()
    refresh()


def serve(engine, port: int = 8080, log_level: str = 'info'):
    from .auth import ChatGPTSession
    from .ai import ResponsesClient
    auth = ChatGPTSession()
    ai_client = ResponsesClient(auth)
    @ui.page('/')
    def index():
        with ui.column().classes('w-full max-w-screen-2xl mx-auto p-6'):
            build_page(engine, ai_client, auth)
    logger.info('Starting fyntok at http://127.0.0.1:%d', port)
    try:
        ui.run(host='127.0.0.1', port=port, title='fyntok', reload=False, show=False,
               uvicorn_logging_level=log_level, access_log=True)
    except Exception:
        logger.exception('fyntok server failed')
        raise
    finally:
        logger.info('fyntok server stopped')
