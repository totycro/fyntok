# Run every required check before committing.
default: check

check: lint typecheck test

lint:
    uv run --locked ruff check .
    uv run --locked ruff format --check .

typecheck:
    uv run --locked ty check .

# Pass pytest arguments, for example: just test -q tests/test_data.py
test *args:
    uv run --locked pytest {{args}}

# Apply safe lint fixes and formatting; review the resulting diff.
format:
    uv run --locked ruff check --fix .
    uv run --locked ruff format .

dev:
    uv run --locked python -m fyntok.cli serve --reload
