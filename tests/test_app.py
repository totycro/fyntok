"""Exercise real NiceGUI callbacks through its built-in user simulator."""
import asyncio

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

from fyntok.app import build_page
from fyntok.database import open_database
from fyntok.importer import import_content
from fyntok.manual import categories
from fyntok.queries import transaction_rows


def test_manual_ui_workflow(tmp_path, caplog):
    engine = open_database(tmp_path / 'ui.sqlite3')

    async def scenario():
        async with user_simulation(lambda: build_page(engine)) as user:
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
        assert not [r for r in caplog.records if r.levelname == 'ERROR']
    finally:
        engine.dispose()
