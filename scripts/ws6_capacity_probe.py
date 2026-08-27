#!/usr/bin/env python3
"""POC-M capacity probe for the Asset Risk reference profile (docs/31 §3).

Measures the four candidate ceilings for ``max_asset_refs`` against the real
PostgreSQL read seam using golden-dataset-shaped portfolios: contract row
bound, P99 result bytes per asset against the pinned result-byte budget, P99
inference-context characters per asset against the graph's fixed context
budget, and the stable read capacity within the statement deadline.  The
closing ceiling is ``floor(min(candidates) * 0.8)``; the probe refuses to
emit a record when any measurement is missing -- there is no implicit
default or "unlimited" fallback.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from statistics import quantiles
from time import perf_counter
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.asset_risk.contracts import AssetStateQuery  # noqa: E402
from app.asset_risk.golden import golden_dataset_hash  # noqa: E402
from app.asset_risk.postgres_adapter import PostgresAssetStateSource  # noqa: E402
from app.asset_risk.read_tool import LARGE_RESULT_BYTES  # noqa: E402

CONTEXT_CHAR_BUDGET = 7_168
STATEMENT_DEADLINE_MS = 5_000
CONTRACT_MAX_ASSETS = 1_024
SAFETY_FACTOR = 0.8
SINGLE_ASSET_REPS = 200
STRESS_REPS = 30
STRESS_SIZES = (1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1_024)


class CapacityProbeError(RuntimeError):
    """A stable, secret-free capacity failure."""


def args_default_seed() -> str:
    runtime = os.environ.get(
        "GROVE_WS6_RUNTIME_URL", "postgresql+psycopg://grove_runtime:grove_runtime_ws0@127.0.0.1:54329/grove"
    )
    return runtime.replace("grove_runtime:grove_runtime_ws0", "grove_migration:grove_migration_ws0")


def _p99(samples: list[float]) -> float:
    if len(samples) < 2:
        raise CapacityProbeError("not enough samples for a percentile")
    ordered = sorted(samples)
    return quantiles(ordered, n=100, method="inclusive")[98]


def closing_ceiling(candidates: dict[str, int]) -> int:
    """floor(min(candidates) * 0.8); every candidate must be a positive int."""

    if set(candidates) != {"rows", "bytes", "context", "deadline"}:
        raise CapacityProbeError(f"candidate set is incomplete: {sorted(candidates)}")
    for name, value in candidates.items():
        if type(value) is not int or value < 1:
            raise CapacityProbeError(f"candidate {name} must be a positive int, got {value!r}")
    raw = min(candidates.values())
    ceiling = int(raw * SAFETY_FACTOR)
    if ceiling < 1:
        raise CapacityProbeError(f"safety factor collapsed the ceiling to {ceiling}")
    return ceiling


async def _seed(engine: Any, tenant: str, size: int) -> tuple[str, ...]:
    refs = tuple(f"asset.capprobe-{index:06d}" for index in range(size))
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO tenant (tenant_id) VALUES (:t) ON CONFLICT DO NOTHING"), {"t": tenant})
        for ref in refs:
            await conn.execute(
                text(
                    "INSERT INTO asset_risk_asset_state (tenant_id, asset_ref, asset_class, exposure_amount, "
                    "currency, status, source_revision) VALUES (:t, :ref, 'credit', 100, 'CNY', 'active', 'rev-1') "
                    "ON CONFLICT DO NOTHING"
                ),
                {"t": tenant, "ref": ref},
            )
    return refs


async def _read_ms(source: PostgresAssetStateSource, tenant: str, refs: tuple[str, ...]) -> float:
    started = perf_counter()
    outcome = await source.read(
        AssetStateQuery(asset_refs=refs),
        tenant_id=tenant,
        logical_read_key=f"capacity:{len(refs)}",
        tool_request_id=uuid4(),
    )
    elapsed = (perf_counter() - started) * 1000
    if not hasattr(outcome, "assets"):
        raise CapacityProbeError(f"read of {len(refs)} refs failed: {getattr(outcome, 'safe_message', outcome)}")
    return elapsed


def _context_chars_per_asset(outcome: Any) -> int:
    """Mirror the graph's per-asset context entries (asset_risk.graph)."""

    entries = [
        {"asset_ref": entry["asset_ref"], "exposure_amount": entry["exposure_amount"]}
        for entry in outcome.model_dump(mode="json")["assets"]
    ]
    per_asset = [len(json.dumps(entry, ensure_ascii=False)) for entry in entries]
    return max(per_asset) if per_asset else 0


async def run_probe(database_url: str, seed_url: str) -> dict[str, Any]:
    tenant = f"ws6-cap-{uuid4().hex[:10]}"
    seed_engine = create_async_engine(seed_url)
    engine = create_async_engine(database_url)
    source = PostgresAssetStateSource(async_sessionmaker(engine, expire_on_commit=False))
    try:
        refs = await _seed(seed_engine, tenant, max(STRESS_SIZES))
        single_ms: list[float] = []
        single_bytes: list[int] = []
        context_chars: list[int] = []
        for _ in range(SINGLE_ASSET_REPS):
            outcome = None
            started = perf_counter()
            outcome = await source.read(
                AssetStateQuery(asset_refs=refs[:1]),
                tenant_id=tenant,
                logical_read_key="capacity:single",
                tool_request_id=uuid4(),
            )
            single_ms.append((perf_counter() - started) * 1000)
            if not hasattr(outcome, "assets"):
                raise CapacityProbeError("single-asset read failed")
            single_bytes.append(len(outcome.model_dump_json()))
            context_chars.append(_context_chars_per_asset(outcome))

        p99_row_ms = _p99(single_ms)
        p99_row_bytes = _p99([float(value) for value in single_bytes])
        p99_context_chars = _p99([float(value) for value in context_chars])

        deadline_capacity = 0
        stress: dict[str, dict[str, float]] = {}
        for size in STRESS_SIZES:
            samples = [await _read_ms(source, tenant, refs[:size]) for _ in range(STRESS_REPS)]
            p99 = _p99(samples)
            stress[str(size)] = {"p50": sorted(samples)[len(samples) // 2], "p99": p99}
            if p99 < STATEMENT_DEADLINE_MS:
                deadline_capacity = size
            else:
                break

        candidates = {
            "rows": CONTRACT_MAX_ASSETS,
            "bytes": int(LARGE_RESULT_BYTES // p99_row_bytes) if p99_row_bytes >= 1 else 0,
            "context": int(CONTEXT_CHAR_BUDGET // p99_context_chars) if p99_context_chars >= 1 else 0,
            "deadline": deadline_capacity,
        }
        ceiling = closing_ceiling(candidates)
        record: dict[str, Any] = {
            "schema": "ws6.poc-m.capacity.v1",
            "golden_dataset_hash": golden_dataset_hash(),
            "environment": {
                "database_url_role": "grove_runtime",
                "seed_url_role": "grove_migration",
                "statement_deadline_ms": STATEMENT_DEADLINE_MS,
                "result_bytes_budget": LARGE_RESULT_BYTES,
                "context_char_budget": CONTEXT_CHAR_BUDGET,
                "contract_max_assets": CONTRACT_MAX_ASSETS,
                "safety_factor": SAFETY_FACTOR,
                "executed_at": datetime.now(UTC).isoformat(),
            },
            "distributions": {
                "single_asset_ms": {"p50": sorted(single_ms)[len(single_ms) // 2], "p99": p99_row_ms},
                "row_bytes": {"p99": p99_row_bytes},
                "context_chars": {"p99": p99_context_chars},
                "stress_by_size": stress,
            },
            "candidates": candidates,
            "closing_ceiling": ceiling,
        }
        body = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        record["report_sha256"] = hashlib.sha256(body).hexdigest()
        return record
    finally:
        await engine.dispose()
        await seed_engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.environ.get(
            "GROVE_WS6_RUNTIME_URL", "postgresql+psycopg://grove_runtime:grove_runtime_ws0@127.0.0.1:54329/grove"
        ),
    )
    parser.add_argument("--output", type=Path, default=ROOT / "ci-evidence/ws6-poc-m-capacity.json")
    parser.add_argument(
        "--seed-database-url",
        default=os.environ.get(
            "GROVE_MIGRATION_DATABASE_URL",
            args_default_seed(),
        ),
    )
    args = parser.parse_args()
    try:
        record = asyncio.run(run_probe(args.database_url, args.seed_database_url))
    except CapacityProbeError as exc:
        print(f"capacity probe failed: {exc}", file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    print(
        json.dumps(
            {
                "closing_ceiling": record["closing_ceiling"],
                "candidates": record["candidates"],
                "report_sha256": record["report_sha256"],
                "output": str(args.output),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
