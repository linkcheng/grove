#!/usr/bin/env python3
"""WS-7 stability probe: golden cases x N against the real provider.

Exit Invariant 1 of the WS-7 task book: run the three frozen golden cases
consecutively N times (default 10); every answer must pass the runtime
structural gate.  A garbage answer now fails closed as the graph's
``inference_output_invalid`` typed failure instead of reaching the report,
so the probe counts a pass only for runs that reached ``terminal``.

Gated exactly like the G3 E2E: without the issued release chain and the
real gateway credential everything fails closed; no mock provider is ever
used.  The report goes to ci-evidence/ (gitignored) and never contains
credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.asset_risk.golden import GOLDEN_CASES, golden_dataset_hash  # noqa: E402

from scripts.ws6_human_review_sheet import REVIEW_PORTFOLIO  # noqa: E402

RUNTIME_URL = os.environ.get(
    "GROVE_WS6_RUNTIME_URL", "postgresql+psycopg://grove_runtime:grove_runtime_ws0@127.0.0.1:54329/grove"
)
MIGRATION_URL = os.environ.get(
    "GROVE_MIGRATION_DATABASE_URL",
    "postgresql+psycopg://grove_migration:grove_migration_ws0@127.0.0.1:54329/grove",
)

_RELEASE_CHAIN_VARS = (
    "AI_GATEWAY_RELEASE_AUTHORITY_DIR",
    "AI_GATEWAY_RELEASE_CANDIDATE_PATH",
    "AI_GATEWAY_RELEASE_EXPECTED_FACTS_PATH",
    "AI_GATEWAY_RELEASE_SIGNATURE_PATH",
    "AI_GATEWAY_PROVIDER_MANIFEST_PATH",
    "AI_GATEWAY_RELEASE_ROOT_PUBLIC_KEY_SHA256",
    "AI_GATEWAY_RELEASE_POLICY_REF",
    "AI_GATEWAY_RELEASE_POLICY_VERSION",
    "AI_GATEWAY_RELEASE_POLICY_SHA256",
)


def _release_chain_configured() -> bool:
    return all(os.environ.get(name) for name in _RELEASE_CHAIN_VARS)


async def _seed(migration_url: str, tenant: str) -> None:
    engine = create_async_engine(migration_url)
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO tenant (tenant_id) VALUES (:t) ON CONFLICT DO NOTHING"), {"t": tenant})
        for ref, values in REVIEW_PORTFOLIO.items():
            await conn.execute(
                text(
                    "INSERT INTO asset_risk_asset_state (tenant_id, asset_ref, asset_class, exposure_amount, "
                    "currency, status, source_revision) VALUES (:t, :ref, :class, :amount, 'CNY', :status, 'rev-1') "
                    "ON CONFLICT DO NOTHING"
                ),
                {
                    "t": tenant,
                    "ref": ref,
                    "class": values["asset_class"],
                    "amount": values["exposure_amount"],
                    "status": values["status"],
                },
            )
    await engine.dispose()


async def probe(
    repeats: int,
    runtime_url: str,
    migration_url: str,
    app_env: str,
    build_hash: str,
    tenant: str,
    pause_seconds: float,
) -> int:
    from app.asset_risk.composition import compose_asset_risk_kernel
    from app.worker.inference import production_inference_lifespan

    await _seed(migration_url, tenant)
    engine = create_async_engine(runtime_url)
    results: list[dict[str, object]] = []
    structural_failures = 0
    try:
        async with production_inference_lifespan(app_env=app_env, runtime_build_hash=build_hash) as (port, factory):
            kernel = compose_asset_risk_kernel(
                inference_port=port,
                inference_request_factory=factory,
                runtime_session_factory=async_sessionmaker(engine, expire_on_commit=False),
                worker_tenant_id=tenant,
            )
            graph: object = kernel.build_graph()
            for repeat in range(1, repeats + 1):
                for case in GOLDEN_CASES:
                    if repeat > 1 or case is not GOLDEN_CASES[0]:
                        # Pace real generations: back-to-back bursts can trip
                        # the gateway's transient rate window, which would
                        # turn an acceptance run into a provider outage
                        # measurement instead of an answer-stability one.
                        await asyncio.sleep(pause_seconds)
                    started = time.monotonic()
                    try:
                        terminal = await graph.ainvoke(  # type: ignore[attr-defined]
                            {
                                "stage": "start",
                                "tenant_id": tenant,
                                "run_id": str(uuid4()),
                                "asset_refs": case.asset_refs,
                            }
                        )
                    except Exception as error:
                        # Adapter-level fail-closed (e.g. unparseable prompted
                        # JSON after the chain's internal retries).  No garbage
                        # reached the user, but no usable answer either.
                        duration = round(time.monotonic() - started, 2)
                        structural_failures += 1
                        results.append(
                            {
                                "repeat": repeat,
                                "case_ref": case.case_ref,
                                "outcome": "inference_error",
                                "error_type": type(error).__name__,
                                "error_detail": str(error)[:200],
                                "duration_s": duration,
                            }
                        )
                        error_name = type(error).__name__
                        print(f"[{repeat}/{repeats}] {case.case_ref}: INFERENCE ERROR {error_name} ({duration}s)")
                        continue
                    duration = round(time.monotonic() - started, 2)
                    stage = terminal.get("stage")
                    if stage == "terminal":
                        answer = str(terminal["report"]["answer"])
                        results.append(
                            {
                                "repeat": repeat,
                                "case_ref": case.case_ref,
                                "outcome": "passed_gate",
                                "answer_chars": len(answer),
                                "duration_s": duration,
                            }
                        )
                        print(f"[{repeat}/{repeats}] {case.case_ref}: passed_gate ({duration}s, {len(answer)} chars)")
                    else:
                        structural_failures += 1
                        results.append(
                            {
                                "repeat": repeat,
                                "case_ref": case.case_ref,
                                "outcome": "structural_failure",
                                "failure_class": terminal.get("failure_class"),
                                "failure_message": terminal.get("failure_message"),
                                "duration_s": duration,
                            }
                        )
                        print(
                            f"[{repeat}/{repeats}] {case.case_ref}: STRUCTURAL FAILURE "
                            f"{terminal.get('failure_class')} ({duration}s)"
                        )
    finally:
        await engine.dispose()
    total = repeats * len(GOLDEN_CASES)
    outcome_counts: dict[str, int] = {}
    for item in results:
        outcome_counts[str(item["outcome"])] = outcome_counts.get(str(item["outcome"]), 0) + 1
    report = {
        "schema": "ws7-stability-probe.v1",
        "dataset_hash": golden_dataset_hash(),
        "repeats": repeats,
        "cases": len(GOLDEN_CASES),
        "total_runs": total,
        # Any non-passing outcome (gate-exhausted garbage or adapter-level
        # fail-closed) counts: the invariant demands stable good answers.
        "structural_failures": structural_failures,
        "outcome_counts": outcome_counts,
        "exit_invariant_1_passed": structural_failures == 0,
        "runs": results,
    }
    evidence_dir = ROOT / "ci-evidence"
    evidence_dir.mkdir(exist_ok=True)
    output = evidence_dir / "ws7-stability-probe.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"runs={total} structural_failures={structural_failures} report={output}")
    return 0 if structural_failures == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument("--pause-seconds", type=float, default=10.0)
    parser.add_argument("--runtime-url", default=RUNTIME_URL)
    parser.add_argument("--migration-url", default=MIGRATION_URL)
    parser.add_argument("--app-env", default=os.environ.get("GROVE_WS6_APP_ENV", "test"))
    parser.add_argument("--runtime-build-hash", default="e" * 64)
    parser.add_argument("--tenant", default="ws7-stability-probe")
    args = parser.parse_args()
    if not _release_chain_configured():
        print("stability probe requires the issued release chain and gateway env", file=sys.stderr)
        return 2
    return asyncio.run(
        probe(
            args.repeats,
            args.runtime_url,
            args.migration_url,
            args.app_env,
            args.runtime_build_hash,
            args.tenant,
            args.pause_seconds,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
