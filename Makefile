PYTHON ?= python3
PIP ?= $(PYTHON) -m pip
PYTEST ?= $(PYTHON) -m pytest

.PHONY: install install-dev test eval-dry-run lint db-check db-upgrade migrate-dry-run migrate verify-migration start stop status logs distributed-start distributed-stop distributed-status distributed-up distributed-down backup

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

distributed-start:
	bash scripts/start_distributed.sh

distributed-stop:
	bash scripts/stop_distributed.sh

distributed-status:
	bash scripts/status_distributed.sh

distributed-up:
	docker compose --profile distributed up -d --build

distributed-down:
	docker compose --profile distributed down

backup:
	bash scripts/backup_data.sh
