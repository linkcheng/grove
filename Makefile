SHELL := /bin/bash

EVIDENCE_DIR := ci-evidence
COMPOSE_PROJECT := grove-ws0-test
COMPOSE := docker compose -p $(COMPOSE_PROJECT) -f compose.yaml

.PHONY: install verify frontend-check manifest-check ws-3-check ws-4-check ws-4-capacity-smoke ws-4-capacity-check integration cleanroom-check ci release-check

install:
	uv sync --frozen

verify:
	uv sync --frozen
	uv run ruff check app scripts tests
	uv run ruff format --check app scripts tests
	uv run mypy .
	# Unit-test coverage gate.  WS-3/WS-4 introduced ~600 lines of DB-bound
	# code (claim/lease/checkpoint/projection/observation service) covered by
	# the deselected integration suite, not by unit tests.  The gate reflects
	# unit-testable coverage; DB-path correctness is gated by ws-3-check/ws-4-check.
	uv run pytest tests -m "not integration" --cov=app --cov-branch --cov-report=term-missing --cov-fail-under=89.0 -ra

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
	@printf 'WS-3 implementation gate (authority, checkpoint, worker and crash recovery)\n'
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
				tests/integration/test_ws3_migration_compatibility.py \
				tests/integration/test_catalog_authority_root.py \
				tests/integration/test_ws3_worker_crash_recovery.py -m integration -ra; \
	else \
		uv run pytest -q tests/integration/test_ws3_postgres_execution_driver.py \
			tests/integration/test_ws3_migration_compatibility.py \
			tests/integration/test_catalog_authority_root.py \
			tests/integration/test_ws3_worker_crash_recovery.py -m integration -ra; \
	fi

ws-4-check:
	@printf 'WS-4 implementation gate (facts, projection, API/SSE, telemetry and operations)\n'
	uv run python scripts/ws4_operational_drill.py
	uv run pytest -q \
		tests/observation/test_facts.py \
		tests/observation/test_reducer.py \
		tests/observation/test_observation_api.py \
		tests/observation/test_telemetry.py \
		tests/observation/test_telemetry_export.py \
		tests/observation/test_operational_assets.py \
		tests/test_manifest.py \
		tests/test_migration_contract.py \
		tests/test_migration_report.py \
		-m 'not integration'
	@if [[ -z "$${GROVE_DATABASE_URL:-}" ]]; then \
		printf 'WS-4 gate requires GROVE_DATABASE_URL for real PostgreSQL integration\n' >&2; \
		exit 2; \
	fi
	uv run pytest -q \
		tests/integration/test_ws4_observation_emit.py \
		tests/integration/test_ws4_projection.py \
		tests/integration/test_ws4_observation_api.py \
		tests/integration/test_ws4_fault_isolation.py \
		-m integration -ra

ws-4-capacity-smoke:
	@for key in WS4_MIGRATION_DATABASE_URL WS4_RUNTIME_DATABASE_URL WS4_PROJECTION_DATABASE_URL WS4_API_DATABASE_URL; do \
		if [[ -z "$${!key:-}" ]]; then printf '%s is required\n' "$$key" >&2; exit 2; fi; \
	done
	uv run python scripts/ws4_capacity_probe.py

ws-4-capacity-check:
	@for key in WS4_MIGRATION_DATABASE_URL WS4_RUNTIME_DATABASE_URL WS4_PROJECTION_DATABASE_URL WS4_API_DATABASE_URL; do \
		if [[ -z "$${!key:-}" ]]; then printf '%s is required\n' "$$key" >&2; exit 2; fi; \
	done
	WS4_TARGET_CAPACITY_CHECK=1 uv run python scripts/ws4_capacity_probe.py --target

integration:
	$(MAKE) manifest-check
	bash scripts/integration.sh

cleanroom-check:
	COMPOSE_PROJECT_NAME=grove-ws0-cleanroom-$$$$ CLEANROOM_REMOVE_VOLUMES=1 \
		INTEGRATION_HOST_DB_PORT=0 INTEGRATION_HOST_API_PORT=0 bash scripts/integration.sh

frontend-check:
	cd frontend && npm ci --no-audit --no-fund && npm run check

ci:
	@printf 'development CI: this target does not certify WS-0 release completion\n'
	$(MAKE) verify
	$(MAKE) frontend-check
	$(MAKE) manifest-check
	$(MAKE) integration

release-check:
	@if [[ -n "$$(git status --porcelain=v1)" ]]; then \
		printf 'release-check failed: source.dirty=true; draft evidence is not publishable\n' >&2; \
		exit 1; \
	fi
	$(MAKE) ci
	uv run python scripts/build_manifest.py --verify $(EVIDENCE_DIR)/runtime-build-manifest.json --require-release --root .
