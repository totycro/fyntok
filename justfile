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
    uv run --locked python -m fyntok.cli serve --host 0.0.0.0 --reload --log-level debug

# Install the pinned local evaluator (requires Node >=22.22).
experiment-install:
    npm --prefix experiments/categorization ci --no-audit --no-fund

# Export a private sample; review every expected_category and enter reviewed=yes.
experiment-export *args:
    uv run --locked python -m fyntok.experimentation export {{args}}

experiment-prepare reviewer *args:
    uv run --locked python -m fyntok.experimentation prepare --reviewer {{quote(reviewer)}} {{args}}

experiment-run *args:
    uv run --locked python -m fyntok.experimentation run {{args}}

# Scripted synthetic responses, no authentication or inference.
experiment-smoke:
    uv run --locked python -m fyntok.experimentation run --offline --dataset experiments/categorization/smoke-fixture.json

experiment-report directory:
    uv run --locked python -m fyntok.experimentation report {{quote(directory)}}

experiment-view:
    PROMPTFOO_CONFIG_DIR="$PWD/data/experiments/promptfoo" PROMPTFOO_DISABLE_TELEMETRY=1 node --require ./experiments/categorization/viewer-bind.cjs experiments/categorization/node_modules/.bin/promptfoo view --no
