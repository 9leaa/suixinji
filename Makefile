PYTHON ?= python3
PIP ?= $(PYTHON) -m pip
PYTEST ?= $(PYTHON) -m pytest

.PHONY: install install-dev test eval-dry-run lint db-check db-upgrade migrate-dry-run migrate verify-migration start stop status logs distributed-start distributed-stop distributed-status distributed-up distributed-down stage4-start stage4-stop stage4-status stage4-validate-basic stage4-chaos-dry-run cutover-check backup

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

stage4-start:
	bash scripts/stage4_processes.sh start

stage4-stop:
	bash scripts/stage4_processes.sh stop

stage4-status:
	bash scripts/stage4_processes.sh status

stage4-validate-basic:
	bash scripts/run_stage4_validation.sh basic

stage4-chaos-dry-run:
	$(PYTHON) scripts/chaos_test_distributed.py

cutover-check:
	$(PYTHON) scripts/check_distributed_cutover.py

backup:
	bash scripts/backup_data.sh
