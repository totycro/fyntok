"""Feature stories. The advisor is scripted so these test product behavior, not model accuracy.

Run with: uv run --locked pytest tests/acceptance -v
"""

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.engine import Engine

from fyntok.categorization import approve_proposal, execute_run, propose_categories, start_run
from fyntok.database import open_database
from fyntok.importer import import_content
from fyntok.manual import (
    assignment_history,
    categories,
    correct_transaction,
    save_category,
    undo_assignment,
)
from fyntok.queries import Filters, transaction_rows


class Advisor:
    """An advisor that initially mistakes supermarket purchases for Shopping."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.while_thinking: Callable[[], object] | None = None
        self.unavailable = False
        self.invalid_answer = False

    def classify(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
        del model
        self.requests.append(payload)
        if self.while_thinking:
            self.while_thinking()
            self.while_thinking = None
        if self.unavailable:
            raise RuntimeError("Plan unavailable")
        shopping = next(c["id"] for c in payload["categories"] if c["name"] == "Shopping")
        answers = []
        for purchase in payload["transactions"]:
            example = next(
                (
                    e
                    for e in payload["confirmed_examples"]
                    if e["description"] == purchase["description"]
                ),
                None,
            )
            answers.append(
                {
                    "transaction_id": purchase["transaction_id"],
                    "category_id": "invented"
                    if self.invalid_answer
                    else example["category_id"]
                    if example
                    else shopping,
                    "reason": "Guided by your confirmed example"
                    if example
                    else "Initial suggestion",
                }
            )
        return {"assignments": answers}

    def propose(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
        del model
        self.requests.append(payload)
        return {
            "categories": [
                {
                    "name": "Groceries",
                    "definition": "Food bought at supermarkets",
                    "kind": "spending",
                }
            ]
        }


class Household:
    """Small vocabulary for the actions a person takes in fyntok."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.advisor = Advisor()
        self.shopping = save_category(engine, "Shopping", "General purchases")
        self.groceries = save_category(engine, "Groceries", "Food at supermarkets")

    def import_purchase(self, merchant: str, day: int) -> None:
        import_content(
            self.engine,
            f"private-account;{merchant};{day:02}.01.2024;{day:02}.01.2024;-10,00;EUR\n".encode(),
            "bank.csv",
        )

    def purchase(self, day: int) -> dict[str, Any]:
        return next(p for p in transaction_rows(self.engine) if p["booking_date"].day == day)

    def confirm(self, day: int, category: str) -> None:
        correct_transaction(self.engine, self.purchase(day)["id"], category)

    def recalculate_history(self, **scope: Any) -> dict[str, Any]:
        run = start_run(self.engine, "story-advisor", Filters(**scope))
        return execute_run(self.engine, self.advisor, run)

    def undo(self, day: int) -> None:
        undo_assignment(self.engine, self.purchase(day)["id"])


@pytest.fixture
def household(tmp_path: Path) -> Iterator[Household]:
    engine = open_database(tmp_path / "household.sqlite3")
    home = Household(engine)
    yield home
    engine.dispose()


def test_one_supermarket_correction_guides_history_without_bulk_editing(
    household: Household,
) -> None:
    """Given two supermarket purchases mistaken for Shopping,
    when I correct one and recalculate history,
    then my decision stays protected and guides the other purchase.
    """
    household.import_purchase("Shop", day=1)
    household.import_purchase("Shop", day=2)
    household.recalculate_history()
    assert household.purchase(1)["category"] == "Shopping"
    assert household.purchase(2)["category"] == "Shopping"

    household.confirm(day=1, category=household.groceries)
    result = household.recalculate_history()

    assert household.purchase(1)["category"] == "Groceries"
    assert household.purchase(1)["protected"]
    assert household.purchase(2)["category"] == "Groceries"
    assert household.purchase(2)["source"] == "ai"
    assert result["applied"] == 1 and result["skipped"] == 1


def test_my_latest_decision_guides_new_imports_and_repeated_recalculation(
    household: Household,
) -> None:
    """When I change my mind, the advisor remembers the latest decision.
    New imports and repeated historical reviews receive that same guidance.
    """
    household.import_purchase("Shop", day=1)
    household.confirm(day=1, category=household.shopping)
    household.confirm(day=1, category=household.groceries)
    household.import_purchase("Shop", day=2)

    household.recalculate_history()
    household.recalculate_history()
    household.import_purchase("Shop", day=3)
    household.recalculate_history()

    assert all(household.purchase(day)["category"] == "Groceries" for day in (1, 2, 3))
    for request in household.advisor.requests:
        assert len(request["confirmed_examples"]) == 1
        assert request["confirmed_examples"][0]["category_id"] == household.groceries


def test_imports_and_all_ai_inputs_use_shared_description_parsing(household: Household) -> None:
    description = r"Bezahlung Karte MC/000000001|EXAMPLE MARKET DANKT 9999\\WIEN\9999|SPESEN: 0,00"
    household.import_purchase(description, day=1)
    household.import_purchase(description, day=2)
    assert household.purchase(1)["merchant"] == "EXAMPLE MARKET"
    assert household.purchase(1)["description"] == description

    propose_categories(household.engine, household.advisor, "story-advisor")
    assert household.advisor.requests[-1]["descriptions"] == ["EXAMPLE MARKET / WIEN"]

    household.confirm(day=1, category=household.groceries)
    result = household.recalculate_history()
    request = household.advisor.requests[-1]
    assert request["transactions"][0]["description"] == "EXAMPLE MARKET / WIEN"
    assert request["confirmed_examples"][0]["description"] == "EXAMPLE MARKET / WIEN"
    assert result["applied"] == 1 and result["skipped"] == 1
    assert household.purchase(1)["protected"]
    assert household.purchase(2)["category"] == "Groceries"


def test_filtered_recalculation_only_revisits_the_selected_history(household: Household) -> None:
    """When I filter to supermarket purchases and recalculate,
    an unrelated pharmacy purchase keeps its previous assignment.
    """
    household.import_purchase("Shop", day=1)
    household.import_purchase("Shop", day=2)
    household.import_purchase("Pharmacy", day=3)
    household.recalculate_history()
    household.confirm(day=1, category=household.groceries)

    household.recalculate_history(merchant="Shop")

    assert household.purchase(2)["category"] == "Groceries"
    assert household.purchase(3)["category"] == "Shopping"


def test_a_decision_made_while_the_advisor_thinks_takes_precedence(household: Household) -> None:
    """When I correct a purchase during an AI request,
    the old answer cannot overwrite my new decision.
    """
    household.import_purchase("Shop", day=1)
    household.advisor.while_thinking = lambda: household.confirm(
        day=1, category=household.groceries
    )

    result = household.recalculate_history()

    assert household.purchase(1)["category"] == "Groceries"
    assert household.purchase(1)["protected"]
    assert result["skipped"] == 1 and result["applied"] == 0


def test_changing_a_definition_during_a_request_discards_the_old_answer(
    household: Household,
) -> None:
    """When I update what Groceries means while categorization is running,
    results based on the previous definitions are excluded.
    """
    household.import_purchase("Shop", day=1)
    household.advisor.while_thinking = lambda: save_category(
        household.engine,
        "Groceries",
        "Food, excluding household supplies",
        category_id=household.groceries,
    )

    result = household.recalculate_history()

    assert household.purchase(1)["category"] == "Uncategorized"
    assert result["skipped"] == 1


def test_ai_reassignment_can_be_undone_to_the_previous_suggestion(household: Household) -> None:
    """After a correction guides historical recalculation,
    I can undo another purchase's AI change and inspect its history.
    """
    household.import_purchase("Shop", day=1)
    household.import_purchase("Shop", day=2)
    household.recalculate_history()
    household.confirm(day=1, category=household.groceries)
    household.recalculate_history()

    household.undo(day=2)

    assert household.purchase(2)["category"] == "Shopping"
    assert household.purchase(2)["origin_source"] == "ai"
    assert len(assignment_history(household.engine, household.purchase(2)["id"])) == 3


def test_category_suggestions_wait_for_my_approval(tmp_path: Path) -> None:
    """An AI proposal is a draft. I can edit it before approving it,
    and only the approved category enters my saved category list.
    """
    engine = open_database(tmp_path / "proposals.sqlite3")
    try:
        suggestions = propose_categories(engine, Advisor(), "story-advisor")
        assert [c["name"] for c in categories(engine)] == ["Uncategorized"]

        suggestions[0]["name"] = "Food and groceries"
        approve_proposal(engine, suggestions[0])

        assert {c["name"] for c in categories(engine)} == {"Uncategorized", "Food and groceries"}
    finally:
        engine.dispose()


@pytest.mark.parametrize("problem", ["unavailable", "invalid_answer"])
def test_an_ai_problem_leaves_imports_and_manual_review_available(
    household: Household, problem: str
) -> None:
    """If my plan is unavailable or the advisor invents a category,
    my bank data stays intact and I can still categorize purchases myself.
    """
    household.import_purchase("Shop", day=1)
    setattr(household.advisor, problem, True)

    result = household.recalculate_history()
    assert result["status"] == "failed"
    assert household.purchase(1)["category"] == "Uncategorized"

    household.confirm(day=1, category=household.groceries)
    assert household.purchase(1)["category"] == "Groceries"


def test_the_next_review_uses_my_updated_category_definition(household: Household) -> None:
    """When I narrow a category's definition and start another review,
    the advisor receives the saved wording for that review.
    """
    household.import_purchase("Shop", day=1)
    household.recalculate_history()
    new_definition = "Food bought for home cooking; exclude household supplies"
    save_category(household.engine, "Groceries", new_definition, category_id=household.groceries)

    household.recalculate_history()

    groceries = next(
        c for c in household.advisor.requests[-1]["categories"] if c["name"] == "Groceries"
    )
    assert groceries["definition"] == new_definition
