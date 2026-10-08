"""Exercise real NiceGUI callbacks through its built-in user simulator."""
import asyncio
import logging
import pytest

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from fyntok.app import build_page
from fyntok.auth import ChatGPTSession
from fyntok.database import open_database
from fyntok.importer import import_content
from fyntok.manual import categories
from fyntok.queries import transaction_rows


def test_manual_ui_workflow(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger='fyntok')
    engine = open_database(tmp_path / 'ui.sqlite3')

    async def scenario():
        async with user_simulation(lambda: build_page(engine, auth=ChatGPTSession(tmp_path / 'credentials'))) as user:
            await user.open('/')
            await user.should_see('0 matching transactions')
            # Upload exercises asynchronous bytes import as used by the browser.
            upload = user.find(ui.upload).elements.pop()
            with user:
                await upload.handle_uploads([ui.upload.SmallFileUpload('bank.csv', 'text/csv',
                    b'a;Supermarket;01.01.2024;02.01.2024;-10,00;EUR\n')])
            await user.should_see('1 matching transactions')
            user.find('Create category').click()
            user.find('Name').type('Food')
            user.find('Definition').type('Groceries and supermarket purchases')
            user.find('Save').click()
            await asyncio.sleep(0.05)
            assert any(c['name'] == 'Food' for c in categories(engine))
            user.find('Transactions').click()
            table = next(t for t in user.find(ui.table).elements if 'description' in {c['name'] for c in t.columns})
            from nicegui import events
            with user:
                listener = next(l for l in table._event_listeners.values() if l.type == 'rowClick')
                events.handle_event(listener.handler, events.GenericEventArguments(sender=table, client=user.client,
                                                                                    args=[{}, table.rows[0], 0]))
            await user.should_see('Transaction details')
            with user:
                selection = next(s for s in user.find(ui.select).elements if s.props.get('label') == 'Category' and s.value == 'uncategorized' and s.visible)
                selection.value = next(c['id'] for c in categories(engine) if c['name'] == 'Food')
            user.find('Confirm individual category').click()
            await asyncio.sleep(0.05)
            row = transaction_rows(engine)[0]
            assert row['category'] == 'Food' and row['protected']
            await user.should_see('1 matching transactions')
            table = next(t for t in user.find(ui.table).elements if 'description' in {c['name'] for c in t.columns})
            with user:
                listener = next(l for l in table._event_listeners.values() if l.type == 'rowClick')
                events.handle_event(listener.handler, events.GenericEventArguments(sender=table, client=user.client,
                                                                                    args=[{}, table.rows[0], 0]))
            user.find('Undo latest assignment change').click()
            await asyncio.sleep(0.05)
            assert transaction_rows(engine)[0]['category'] == 'Uncategorized'
            assert not transaction_rows(engine)[0]['protected']
            user.find('Description contains').type('missing')
            user.find('Apply filters').click()
            await asyncio.sleep(0.05)
            await user.should_see('0 matching transactions')
            user.find('Clear').click()
            await asyncio.sleep(0.05)
            await user.should_see('1 matching transactions')
            user.find('Categories').click()
            user.find('Edit').click()
            user.find('Name').clear().type('Groceries')
            user.find('Save').click()
            await asyncio.sleep(0.05)
            assert any(c['name'] == 'Groceries' for c in categories(engine))
            user.find('Create category').click()
            user.find('Name').type('Shopping')
            user.find('Save').click()
            await asyncio.sleep(0.05)
            user.find('Merge').click()
            with user:
                destination = next(s for s in user.find(ui.select).elements if s.props.get('label') == 'Destination')
                destination.value = next(c['id'] for c in categories(engine) if c['name'] == 'Shopping')
            user.find('Merge category').click()
            await asyncio.sleep(0.05)
            assert {c['name'] for c in categories(engine)} == {'Shopping', 'Uncategorized'}
            user.find('Delete').click()
            user.find('Delete category').click()
            await asyncio.sleep(0.05)
            assert [c['name'] for c in categories(engine)] == ['Uncategorized']
    try:
        asyncio.run(scenario())
        for action in ('Create category', 'Edit category', 'Merge category', 'Delete category',
                       'Confirm individual category', 'Undo latest assignment change'):
            assert f'{action}: completed' in caplog.text
        assert 'Apply filters: refreshing' in caplog.text
        assert 'Clear filters: resetting' in caplog.text
        assert not [r for r in caplog.records if r.levelname == 'ERROR']
    finally:
        engine.dispose()


def test_ai_ui_proposals_model_selection_and_recalculation(tmp_path, caplog):
    """A person connects their plan, edits and approves a suggestion, and reviews AI assignments."""
    from fyntok.auth import ChatGPTSession
    caplog.set_level(logging.DEBUG, logger='fyntok')
    engine = open_database(tmp_path / 'ai-ui.sqlite3')
    import_content(engine, b'a;EXAMPLE GROCERY;01.01.2024;01.01.2024;-10,00;EUR\n', 'bank.csv')
    auth = ChatGPTSession(tmp_path / 'credentials')
    with auth.locked():
        data = auth.load()
        data['accounts']['client'] = dict(subject='person', email='person@example.com')
        data['active'] = 'client'
        auth.save(data)

    auth.start_login = lambda client_id=None: 'https://auth.openai.com/api/accounts/authorize?state=test'

    class Advisor:
        def models(self):
            return [dict(slug='story-model', display_name='Story model')]

        def propose(self, model, payload):
            assert 'Propose categories: preparing descriptions and requesting AI suggestions' in caplog.text
            assert model == 'story-model'
            return {'categories': [dict(name='Groceries', definition='Food at supermarkets', kind='spending')]}

        def classify(self, model, payload):
            category = next(c for c in payload['categories'] if c['name'] == 'Food')
            return {'assignments': [dict(transaction_id=t['transaction_id'], category_id=category['id'], reason='Supermarket')
                                    for t in payload['transactions']]}

    async def scenario():
        async with user_simulation(lambda: build_page(engine, Advisor(), auth)) as user:
            await user.open('/')
            user.find('Propose categories').click()
            await user.should_see('Choose a model and wait for current AI work to finish.')
            assert 'Propose categories: blocked; busy=False, model selected=False' in caplog.text
            user.find(kind=ui.button, content='Load models').click()
            await user.should_see('ChatGPT plan connected.')
            user.find('Propose categories').click()
            await user.should_see('1 suggestions ready for review.')
            assert len(categories(engine)) == 1
            user.find('Suggested name').clear().type('Food')
            user.find('Approve category').click()
            await asyncio.sleep(0.05)
            assert {c['name'] for c in categories(engine)} == {'Food', 'Uncategorized'}
            user.find('Recalculate categories — all transactions').click()
            await user.should_see('Completed: 1 applied, 0 protected or changed; 1 / 1 processed.')
            assert transaction_rows(engine)[0]['category'] == 'Food'
            assert transaction_rows(engine)[0]['source'] == 'ai'
            user.find('Sign out').click()
            await user.should_see('Signed out.')
    try:
        asyncio.run(scenario())
        assert 'Load models: completed with 1 available models' in caplog.text
        assert 'Propose categories: completed with 1 suggestions' in caplog.text
        assert 'Approve category: completed' in caplog.text
        assert 'Recalculate categories — all transactions: saved run' in caplog.text
        assert 'Categorization run progress: status=completed' in caplog.text
        assert 'Sign out: completed' in caplog.text
        assert 'person@example.com' not in caplog.text
        assert not [r for r in caplog.records if r.levelname == 'ERROR']
    finally:
        engine.dispose()


@pytest.mark.parametrize('error_type', [ValueError, RuntimeError])
def test_proposal_failure_logs_traceback_at_default_level(tmp_path, caplog, error_type):
    caplog.set_level(logging.INFO, logger='fyntok')
    engine = open_database(tmp_path / 'failed-proposal.sqlite3')

    class Advisor:
        def models(self):
            return [dict(slug='test-model', display_name='Test model')]

        def propose(self, model, payload):
            raise error_type('proposal request failed')

    async def scenario():
        async with user_simulation(lambda: build_page(
                engine, Advisor(), ChatGPTSession(tmp_path / 'credentials'))) as user:
            await user.open('/')
            user.find(kind=ui.button, content='Load models').click()
            await user.should_see('ChatGPT plan connected.')
            user.find('Propose categories').click()
            await user.should_see(f'Category suggestions unavailable ({error_type.__name__}).')

    try:
        asyncio.run(scenario())
        record = next(r for r in caplog.records if r.getMessage().startswith('Propose categories failed'))
        assert record.levelno == logging.ERROR
        assert record.exc_info and record.exc_info[0] is error_type
        assert 'Traceback (most recent call last):' in caplog.text
        assert f'{error_type.__name__}: proposal request failed' in caplog.text
        assert 'in propose' in caplog.text
        assert 'Propose categories: completed' not in caplog.text
    finally:
        engine.dispose()


def test_ui_cancel_and_recover_after_reload(tmp_path, caplog):
    from fyntok.categorization import cancel_run, start_run
    engine = open_database(tmp_path / 'recover-ui.sqlite3')
    import_content(engine, b'a;EXAMPLE GROCERY;01.01.2024;01.01.2024;-10,00;EUR\n', 'bank.csv')
    auth = ChatGPTSession(tmp_path / 'credentials')
    run_id = start_run(engine, 'saved-model')
    class Advisor:
        def classify(self, model, payload):
            assert model == 'saved-model'
            cancel_run(engine, run_id)
            return {'assignments': [dict(transaction_id=t['transaction_id'], category_id='uncategorized', reason='Unclear') for t in payload['transactions']]}
    async def scenario():
        async with user_simulation(lambda: build_page(engine, Advisor(), auth)) as user:
            await user.open('/')
            await user.should_see('Pending: 0 applied')
            user.find('Cancel selected run').click()
            await user.should_see('Cancelled: 0 applied')
        class Recovered(Advisor):
            def classify(self, model, payload):
                assert model == 'saved-model'
                return {'assignments': [dict(transaction_id=t['transaction_id'], category_id='uncategorized', reason='Unclear') for t in payload['transactions']]}
        async with user_simulation(lambda: build_page(engine, Recovered(), auth)) as user:
            await user.open('/')
            await user.should_see('Cancelled: 0 applied')
            user.find('Resume / recover selected run').click()
            await user.should_see('Completed: 1 applied')
            # Polling completed history must leave sign-out status visible.
            user.find('Sign out').click()
            await user.should_see('Signed out.')
            await asyncio.sleep(0.6)
            await user.should_see('Signed out.')
    try:
        asyncio.run(scenario())
        assert not [r for r in caplog.records if r.levelname == 'ERROR']
    finally:
        engine.dispose()
