.PHONY: setup profile ingest validate marts analyze validate-analytics lint test check db-up db-down migrate

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

marts:
	uv run instacart marts --json-report "$(ANALYTICS_REPORT)" --markdown-report "$(ANALYTICS_MARKDOWN_REPORT)"

analyze:
	uv run instacart analyze --json-report "$(ANALYTICS_REPORT)" --markdown-report "$(ANALYTICS_MARKDOWN_REPORT)" --random-seed "$(ANALYTICS_RANDOM_SEED)" --bootstrap-replicates "$(BOOTSTRAP_REPLICATES)" --bootstrap-customer-sample "$(BOOTSTRAP_CUSTOMER_SAMPLE)" --silhouette-sample-size "$(SILHOUETTE_SAMPLE_SIZE)"

validate-analytics:
	uv run instacart validate-analytics --report "$(ANALYTICS_VALIDATION_REPORT)"

lint:
	uv run ruff check .
	uv run ruff format --check .

test:
	uv run pytest

check: lint test

DATASET_PATH ?= data/raw
PROFILE_REPORT ?= reports/profile.json
VALIDATION_REPORT ?= reports/validation.json
ANALYTICS_REPORT ?= reports/analytics.json
ANALYTICS_MARKDOWN_REPORT ?= reports/analytics.md
ANALYTICS_VALIDATION_REPORT ?= reports/analytics-validation.json
ANALYTICS_RANDOM_SEED ?= 2026
BOOTSTRAP_REPLICATES ?= 200
BOOTSTRAP_CUSTOMER_SAMPLE ?= 10000
SILHOUETTE_SAMPLE_SIZE ?= 5000
