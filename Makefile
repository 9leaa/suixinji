PYTHON ?= python3
PIP ?= $(PYTHON) -m pip
PYTEST ?= $(PYTHON) -m pytest

.PHONY: install install-dev test eval-dry-run lint db-check db-upgrade migrate-dry-run migrate verify-migration start stop status logs backup

install:
	$(PIP) install -r requirements.txt

install-dev:
	$(PIP) install -r requirements-dev.txt

test:
	$(PYTEST) tests --cov=. --cov-report=term-missing

eval-dry-run:
	STORAGE_BACKEND=local $(PYTHON) eval/eval_classification.py --dry-run
	STORAGE_BACKEND=local $(PYTHON) eval/eval_retrieval.py --dry-run
	STORAGE_BACKEND=local $(PYTHON) eval/eval_summary.py --dry-run
	STORAGE_BACKEND=local $(PYTHON) eval/eval_query_react.py --dry-run
	STORAGE_BACKEND=local $(PYTHON) eval/eval_memory.py --dry-run

lint:
	$(PYTHON) -m ruff check .

db-check:
	$(PYTHON) scripts/check_database.py

db-upgrade:
	$(PYTHON) -m alembic upgrade head

migrate-dry-run:
	$(PYTHON) scripts/migrate_local_to_postgres.py --dry-run

migrate:
	$(PYTHON) scripts/migrate_local_to_postgres.py

verify-migration:
	$(PYTHON) scripts/verify_migration.py

start:
	bash scripts/start.sh

stop:
	bash scripts/stop.sh

status:
	bash scripts/status.sh

logs:
	bash scripts/logs.sh

backup:
	bash scripts/backup_data.sh
