PYTHON ?= python3
PIP ?= $(PYTHON) -m pip
PYTEST ?= $(PYTHON) -m pytest

.PHONY: install install-dev test eval-dry-run lint db-check db-upgrade migrate-dry-run migrate verify-migration start stop status logs distributed-start distributed-stop distributed-status distributed-up distributed-down stage4-up stage4-down stage4-status stage4-load-smoke stage4-load-basic stage4-ephemeral-up stage4-ephemeral-down stage4-ephemeral-load-basic stage4-chaos-dry-run cutover-check backup

install:
	$(PIP) install -r requirements.txt

install-dev:
	$(PIP) install -r requirements-dev.txt

test:
	$(PYTEST) tests --cov=. --cov-report=term-missing

eval-dry-run:
	STORAGE_BACKEND=local COORDINATION_BACKEND=local TASK_QUEUE_BACKEND=local $(PYTHON) eval/eval_classification.py --dry-run
	STORAGE_BACKEND=local COORDINATION_BACKEND=local TASK_QUEUE_BACKEND=local $(PYTHON) eval/eval_retrieval.py --dry-run
	STORAGE_BACKEND=local COORDINATION_BACKEND=local TASK_QUEUE_BACKEND=local $(PYTHON) eval/eval_summary.py --dry-run
	STORAGE_BACKEND=local COORDINATION_BACKEND=local TASK_QUEUE_BACKEND=local $(PYTHON) eval/eval_query_react.py --dry-run
	STORAGE_BACKEND=local COORDINATION_BACKEND=local TASK_QUEUE_BACKEND=local $(PYTHON) eval/eval_memory.py --dry-run

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

stage4-up:
	bash scripts/stage4_compose.sh up

stage4-down:
	bash scripts/stage4_compose.sh down

stage4-status:
	bash scripts/stage4_compose.sh status

stage4-load-smoke:
	bash scripts/stage4_compose.sh load smoke

stage4-load-basic:
	bash scripts/stage4_compose.sh load basic

stage4-ephemeral-up:
	bash scripts/stage4_compose.sh ephemeral-up

stage4-ephemeral-down:
	bash scripts/stage4_compose.sh ephemeral-down

stage4-ephemeral-load-basic:
	bash scripts/stage4_compose.sh ephemeral-load basic

stage4-chaos-dry-run:
	$(PYTHON) scripts/chaos_test_distributed.py

cutover-check:
	$(PYTHON) scripts/check_distributed_cutover.py

backup:
	bash scripts/backup_data.sh
