.PHONY: setup profile ingest validate lint test check db-up db-down migrate

setup:
	uv sync --locked --python 3.11 --all-groups
	docker compose up -d --wait postgres
	$(MAKE) migrate

db-up:
	docker compose up -d postgres

db-down:
	docker compose stop postgres

migrate:
	uv run alembic upgrade head

profile:
	uv run instacart profile --dataset "$(DATASET_PATH)" --report "$(PROFILE_REPORT)"

ingest:
	uv run instacart ingest --dataset "$(DATASET_PATH)" --report "$(PROFILE_REPORT)" $(if $(filter 1 true yes,$(FORCE)),--force,)

validate:
	uv run instacart validate --report "$(VALIDATION_REPORT)"

lint:
	uv run ruff check .
	uv run ruff format --check .

test:
	uv run pytest

check: lint test

DATASET_PATH ?= data/raw
PROFILE_REPORT ?= reports/profile.json
VALIDATION_REPORT ?= reports/validation.json
