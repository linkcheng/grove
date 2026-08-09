SHELL := /bin/bash

EVIDENCE_DIR := ci-evidence
COMPOSE_PROJECT := grove-ws0-test
COMPOSE := docker compose -p $(COMPOSE_PROJECT) -f compose.yaml

.PHONY: install verify manifest-check ws-3-check integration cleanroom-check ci release-check

install:
	uv sync --frozen

verify:
	uv sync --frozen
	uv run ruff check app scripts tests
	uv run ruff format --check app scripts tests
	uv run mypy .
	uv run pytest tests -m "not integration" --cov=app --cov-branch --cov-report=term-missing --cov-fail-under=91.84 -ra

manifest-check:
	mkdir -p $(EVIDENCE_DIR)
	uv run python scripts/dependency_report.py
	uv run python scripts/build_manifest.py --output $(EVIDENCE_DIR)/runtime-build-manifest.json
	uv run python scripts/build_manifest.py --output $(EVIDENCE_DIR)/runtime-build-manifest.second.json
	cmp $(EVIDENCE_DIR)/runtime-build-manifest.json $(EVIDENCE_DIR)/runtime-build-manifest.second.json
	uv run python scripts/build_manifest.py --verify $(EVIDENCE_DIR)/runtime-build-manifest.json
	rm -f $(EVIDENCE_DIR)/runtime-build-manifest.second.json
	bash scripts/reverse_validation.sh

ws-3-check:
	@printf 'WS-3 current gate (checkpoint, cancel, execution-authority closure candidates; not full WS-3)\n'
	uv run pytest -q \
		tests/test_ws3_checkpoint.py \
		tests/test_ws3_execution_driver.py \
		tests/test_ws3_execution_state_machine.py \
		tests/test_ws3_postgres_execution_driver.py \
		tests/integration/test_catalog_authority_root.py \
		tests/test_manifest.py \
		tests/test_migration_contract.py \
		tests/test_migration_report.py \
		-m 'not integration'
	@if [[ -z "$${GROVE_DATABASE_URL:-}" ]]; then \
		printf 'WS-3 current gate requires GROVE_DATABASE_URL for real PostgreSQL integration\n' >&2; \
		exit 2; \
	fi
	@if [[ -n "$${GROVE_MIGRATION_DATABASE_URL:-}" ]]; then \
		GROVE_ROLE=api uv run python scripts/ws3_preflight.py \
			--database-url "$${GROVE_MIGRATION_DATABASE_URL}"; \
	else \
		GROVE_ROLE=api uv run python scripts/ws3_preflight.py \
			--database-url "$${GROVE_DATABASE_URL}"; \
	fi
	@if [[ -n "$${GROVE_MIGRATION_DATABASE_URL:-}" ]]; then \
		GROVE_MIGRATION_DATABASE_URL="$$GROVE_MIGRATION_DATABASE_URL" \
			uv run pytest -q tests/integration/test_ws3_postgres_execution_driver.py \
				tests/integration/test_catalog_authority_root.py -m integration -ra; \
	else \
		uv run pytest -q tests/integration/test_ws3_postgres_execution_driver.py \
			tests/integration/test_catalog_authority_root.py -m integration -ra; \
	fi

integration:
	$(MAKE) manifest-check
	bash scripts/integration.sh

cleanroom-check:
	COMPOSE_PROJECT_NAME=grove-ws0-cleanroom-$$$$ CLEANROOM_REMOVE_VOLUMES=1 bash scripts/integration.sh

ci:
	@printf 'development CI: this target does not certify WS-0 release completion\n'
	$(MAKE) verify
	$(MAKE) manifest-check
	$(MAKE) integration

release-check:
	@if [[ -n "$$(git status --porcelain=v1)" ]]; then \
		printf 'release-check failed: source.dirty=true; draft evidence is not publishable\n' >&2; \
		exit 1; \
	fi
	$(MAKE) ci
	uv run python scripts/build_manifest.py --verify $(EVIDENCE_DIR)/runtime-build-manifest.json --require-release --root .
