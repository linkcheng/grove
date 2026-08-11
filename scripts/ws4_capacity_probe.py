#!/usr/bin/env python3
"""Executable WS-4 resource and telemetry-fault probe.

The smoke profile exercises the same real seams with a short outage window.
``--target`` uses every value from ``ops/ws4-reference-target.json`` and is the
only mode that may claim reference-target evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import quantiles
from time import monotonic, perf_counter
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
from app.observation.projection import ProjectionReconciler
from psycopg.types.json import Jsonb
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[1]
TARGET_PATH = ROOT / "ops/ws4-reference-target.json"


class CapacityProbeError(RuntimeError):
    """A stable, secret-free capacity failure."""


@dataclass(frozen=True, slots=True)
class ProbeProfile:
    mode: str
    event_count: int
    recovery_event_count: int
    event_rate: int
    sse_connections: int
    telemetry_outage_seconds: float
    telemetry_baseline_seconds: float
    max_event_bytes: int
    max_sse_p95_seconds: float
    max_projection_p95_seconds: float
    max_projection_recovery_seconds: float
    max_latency_regression_ratio: float


def _load_profile(target_mode: bool) -> ProbeProfile:
    target = json.loads(TARGET_PATH.read_text(encoding="utf-8"))
    if target_mode:
        if os.environ.get("WS4_TARGET_CAPACITY_CHECK") != "1":
            raise CapacityProbeError("target mode requires WS4_TARGET_CAPACITY_CHECK=1")
        return ProbeProfile(
            mode="reference_target_v1",
            event_count=max(2_000, int(target["events_per_second"]) * 10),
            recovery_event_count=1_000,
            event_rate=int(target["events_per_second"]),
            sse_connections=int(target["sse_connections"]),
            telemetry_outage_seconds=float(target["telemetry_outage_minutes"]) * 60,
            telemetry_baseline_seconds=30.0,
            max_event_bytes=int(target["max_event_bytes"]),
            max_sse_p95_seconds=float(target["runtime_event_to_sse_p95_seconds"]),
            max_projection_p95_seconds=float(target["projection_normal_p95_seconds"]),
            max_projection_recovery_seconds=float(target["projection_recovery_seconds"]),
            max_latency_regression_ratio=float(target["max_online_latency_regression_ratio"]),
        )
    return ProbeProfile(
        mode="smoke",
        event_count=400,
        recovery_event_count=100,
        event_rate=int(target["events_per_second"]),
        sse_connections=25,
        telemetry_outage_seconds=3.0,
        telemetry_baseline_seconds=3.0,
        max_event_bytes=int(target["max_event_bytes"]),
        max_sse_p95_seconds=float(target["runtime_event_to_sse_p95_seconds"]),
        max_projection_p95_seconds=float(target["projection_normal_p95_seconds"]),
        max_projection_recovery_seconds=float(target["projection_recovery_seconds"]),
        # Smoke only proves bounded non-blocking behavior; the exact 10%
        # degradation threshold requires the 15-minute target sample.
        max_latency_regression_ratio=1.0,
    )


def _load_geometry_profile() -> ProbeProfile:
    target = json.loads(TARGET_PATH.read_text(encoding="utf-8"))
    return ProbeProfile(
        mode="target_geometry_smoke",
        event_count=max(2_000, int(target["events_per_second"]) * 10),
        recovery_event_count=1_000,
        event_rate=int(target["events_per_second"]),
        sse_connections=int(target["sse_connections"]),
        telemetry_outage_seconds=3.0,
        telemetry_baseline_seconds=3.0,
        max_event_bytes=int(target["max_event_bytes"]),
        max_sse_p95_seconds=float(target["runtime_event_to_sse_p95_seconds"]),
        max_projection_p95_seconds=float(target["projection_normal_p95_seconds"]),
        max_projection_recovery_seconds=float(target["projection_recovery_seconds"]),
        max_latency_regression_ratio=1.0,
    )


def _database_urls() -> dict[str, str]:
    names = {
        "migration": "WS4_MIGRATION_DATABASE_URL",
        "runtime": "WS4_RUNTIME_DATABASE_URL",
        "projection": "WS4_PROJECTION_DATABASE_URL",
        "api": "WS4_API_DATABASE_URL",
    }
    missing = [env_name for env_name in names.values() if not os.environ.get(env_name)]
    if missing:
        raise CapacityProbeError(f"missing required environment key: {missing[0]}")
    return {role: os.environ[env_name] for role, env_name in names.items()}


def _psycopg_url(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)


def _percentile(values: list[float], percentile: int) -> float:
    if not values:
        raise CapacityProbeError("latency sample is empty")
    if len(values) == 1:
        return values[0]
    return quantiles(values, n=100, method="inclusive")[percentile - 1]


async def _seed_run(database_url: str, tenant_id: str, run_id: UUID) -> None:
    engine = create_async_engine(database_url)
    digest = run_id.hex.ljust(64, "0")[:64]
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO tenant (tenant_id) VALUES (:tenant)"),
                {"tenant": tenant_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO membership (tenant_id, principal_id, principal_kind, user_ref, roles, active) "
                    "VALUES (:tenant, 'capacity-user', 'human', 'capacity-user', "
                    "'[\"execution.query\"]'::jsonb, true)"
                ),
                {"tenant": tenant_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO execution_principal (tenant_id, principal_id, principal_kind) "
                    "VALUES (:tenant, 'capacity-user', 'human') ON CONFLICT DO NOTHING"
                ),
                {"tenant": tenant_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO execution_spec (tenant_id, skill_spec_hash, spec_ref, spec_payload) "
                    "VALUES (:tenant, :hash, :ref, CAST(:payload AS jsonb))"
                ),
                {
                    "tenant": tenant_id,
                    "hash": "b" * 64,
                    "ref": "execution-spec:" + "b" * 64,
                    "payload": json.dumps({"capacity": True}),
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO agent_run (tenant_id, run_id, submission_id, submission_digest, "
                    "principal_id, principal_kind, skill_spec_hash, skill_spec_ref, runtime_build_ref, "
                    "runtime_build_hash, status, revision) VALUES (:tenant, :run_id, :submission_id, :digest, "
                    "'capacity-user', 'human', :spec_hash, :spec_ref, 'capacity-build', :runtime_hash, "
                    "'running', 1)"
                ),
                {
                    "tenant": tenant_id,
                    "run_id": run_id,
                    "submission_id": uuid4(),
                    "digest": digest,
                    "spec_hash": "b" * 64,
                    "spec_ref": "execution-spec:" + "b" * 64,
                    "runtime_hash": "a" * 64,
                },
            )
    finally:
        await engine.dispose()


def _event_descriptors(run_id: UUID, start: int, count: int, occurred_at: datetime) -> list[dict[str, Any]]:
    return [
        {
            "event_type": "run.lifecycle",
            "source": "grove.runtime_worker",
            "source_event_id": f"capacity:{run_id}:{sequence}",
            "payload_schema_ref": "grove.runtime.run-lifecycle.v1",
            "payload": {
                "kind": "run_lifecycle",
                "run_id": str(run_id),
                "status": "running",
                "run_revision": sequence,
            },
            "occurred_at": occurred_at.isoformat(),
        }
        for sequence in range(start, start + count)
    ]


async def _emit_events(
    database_url: str,
    tenant_id: str,
    run_id: UUID,
    *,
    start: int,
    count: int,
    events_per_second: int | None = None,
) -> tuple[float, datetime]:
    first_occurred_at: datetime | None = None
    started = perf_counter()
    try:
        async with await psycopg.AsyncConnection.connect(_psycopg_url(database_url)) as connection:
            for offset in range(0, count, 32):
                if events_per_second is not None:
                    scheduled_at = started + (offset / events_per_second)
                    await asyncio.sleep(max(0.0, scheduled_at - perf_counter()))
                batch_size = min(32, count - offset)
                occurred_at = datetime.now(UTC)
                if first_occurred_at is None:
                    first_occurred_at = occurred_at
                descriptors = _event_descriptors(run_id, start + offset, batch_size, occurred_at)
                async with connection.transaction():
                    await connection.execute(
                        "SELECT set_config('grove.tenant_id', %s, true)",
                        (tenant_id,),
                    )
                    cursor = await connection.execute(
                        "SELECT * FROM grove_emit_runtime_events(%s, %s, %s, %s, %s, %s, %s)",
                        (tenant_id, run_id, run_id, str(run_id), run_id, None, Jsonb(descriptors)),
                    )
                    await cursor.fetchall()
    except Exception as exc:
        raise CapacityProbeError(f"runtime event emission failed: {type(exc).__name__}") from None
    if first_occurred_at is None:
        raise CapacityProbeError("runtime event batch must not be empty")
    return perf_counter() - started, first_occurred_at


async def _tenant_counts(database_url: str, tenant_id: str) -> tuple[int, int, int]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT "
                        "(SELECT count(*) FROM runtime_event WHERE tenant_id = :tenant), "
                        "(SELECT count(*) FROM runtime_event_outbox WHERE tenant_id = :tenant "
                        "AND relayed_at IS NOT NULL), "
                        "(SELECT count(*) FROM ui_projection_event WHERE tenant_id = :tenant)"
                    ),
                    {"tenant": tenant_id},
                )
            ).one()
            return int(row[0]), int(row[1]), int(row[2])
    finally:
        await engine.dispose()


async def _wait_for_projection(
    database_url: str,
    tenant_id: str,
    expected: int,
    timeout_seconds: float,
) -> None:
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        _, relayed, projected = await _tenant_counts(database_url, tenant_id)
        if relayed >= expected and projected >= expected:
            return
        await asyncio.sleep(0.05)
    raise CapacityProbeError("projection did not reach the durable watermark before timeout")


async def _projection_p95(database_url: str, tenant_id: str) -> float:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            value = (
                await connection.execute(
                    text(
                        "SELECT percentile_cont(0.95) WITHIN GROUP ("
                        "ORDER BY EXTRACT(EPOCH FROM (recorded_at - projected_at))) "
                        "FROM ui_projection_event WHERE tenant_id = :tenant"
                    ),
                    {"tenant": tenant_id},
                )
            ).scalar_one()
            return float(value)
    finally:
        await engine.dispose()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def _wait_api(base_url: str, process: subprocess.Popen[bytes]) -> None:
    async with httpx.AsyncClient(timeout=0.5, trust_env=False) as client:
        for _ in range(100):
            if process.poll() is not None:
                raise CapacityProbeError("capacity API process exited before readiness")
            try:
                response = await client.get(f"{base_url}/api/v1/health/live")
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.05)
    raise CapacityProbeError("capacity API process did not become ready")


def _start_api(
    database_url: str,
    port: int,
    *,
    otlp_endpoint: str | None = None,
) -> subprocess.Popen[bytes]:
    environment = dict(os.environ)
    for key in tuple(environment):
        if key.startswith("GROVE_"):
            environment.pop(key)
    environment.update(
        {
            "GROVE_ROLE": "api",
            "GROVE_APP_ENV": "integration",
            "GROVE_AUTH_MODE": "fixture",
            "GROVE_DATABASE_URL": database_url,
            "GROVE_DATABASE_POOL_SIZE": "50",
            "GROVE_DATABASE_MAX_OVERFLOW": "0",
            "GROVE_DATABASE_POOL_TIMEOUT_SECONDS": "5",
        }
    )
    if otlp_endpoint is None:
        environment.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
    else:
        environment["OTEL_EXPORTER_OTLP_ENDPOINT"] = otlp_endpoint
    return subprocess.Popen(  # noqa: S603 -- fixed interpreter/module/host; only OS-assigned numeric port varies.
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _stop_api(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


async def _sse_probe(
    profile: ProbeProfile,
    urls: dict[str, str],
    tenant_id: str,
    run_id: UUID,
    cursor: int,
    next_event_sequence: int,
) -> float:
    port = _free_port()
    process = _start_api(urls["api"], port)
    base_url = f"http://127.0.0.1:{port}"
    try:
        await _wait_api(base_url, process)
        limits = httpx.Limits(
            max_connections=profile.sse_connections + 20,
            max_keepalive_connections=0,
        )
        timeout = httpx.Timeout(20.0, connect=10.0)
        headers = {"authorization": f"Bearer fixture:{tenant_id}:capacity-user"}
        latencies: list[float] = []
        triggered_at = 0.0

        async with httpx.AsyncClient(limits=limits, timeout=timeout, trust_env=False) as client:

            async def receive_one() -> None:
                nonlocal triggered_at
                url = f"{base_url}/api/v1/observations/runs/{run_id}/ui/stream"
                async with client.stream(
                    "GET",
                    url,
                    params={"after_projection_seq": cursor},
                    headers=headers,
                ) as response:
                    if response.status_code != 200:
                        raise CapacityProbeError("SSE endpoint rejected a capacity connection")
                    async for line in response.aiter_lines():
                        if line.startswith("data:"):
                            latencies.append(monotonic() - triggered_at)
                            return
                raise CapacityProbeError("SSE connection ended without a durable event")

            tasks = [asyncio.create_task(receive_one()) for _ in range(profile.sse_connections)]
            await asyncio.sleep(1.0)
            triggered_at = monotonic()
            await _emit_events(
                urls["runtime"],
                tenant_id,
                run_id,
                start=next_event_sequence,
                count=1,
            )
            reconciler = ProjectionReconciler(
                async_sessionmaker(create_async_engine(urls["projection"]), expire_on_commit=False)
            )
            await reconciler.run_once()
            try:
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=20.0)
            except Exception:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
        if len(latencies) != profile.sse_connections:
            raise CapacityProbeError("not every SSE connection received the committed event")
        return _percentile(latencies, 95)
    except CapacityProbeError:
        raise
    except Exception as exc:
        raise CapacityProbeError(f"SSE capacity probe failed: {type(exc).__name__}") from None
    finally:
        _stop_api(process)


async def _online_latency_sample(base_url: str, duration_seconds: float) -> list[float]:
    samples: list[float] = []
    deadline = monotonic() + duration_seconds
    async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
        while monotonic() < deadline:
            iteration_started = monotonic()
            response = await client.get(f"{base_url}/api/v1/health/ready")
            if response.status_code != 200:
                raise CapacityProbeError("online readiness request failed during telemetry probe")
            samples.append(monotonic() - iteration_started)
            await asyncio.sleep(max(0.0, 0.04 - (monotonic() - iteration_started)))
    return samples


async def _sample_api_process(
    database_url: str,
    duration_seconds: float,
    *,
    otlp_endpoint: str | None,
) -> list[float]:
    port = _free_port()
    process = _start_api(database_url, port, otlp_endpoint=otlp_endpoint)
    base_url = f"http://127.0.0.1:{port}"
    try:
        await _wait_api(base_url, process)
        return await _online_latency_sample(base_url, duration_seconds)
    finally:
        _stop_api(process)


async def _telemetry_outage_probe(profile: ProbeProfile, api_database_url: str) -> dict[str, float]:
    baseline_before = await _sample_api_process(
        api_database_url,
        profile.telemetry_baseline_seconds,
        otlp_endpoint=None,
    )
    outage = await _sample_api_process(
        api_database_url,
        profile.telemetry_outage_seconds,
        otlp_endpoint="http://127.0.0.1:1",
    )
    baseline_after = await _sample_api_process(
        api_database_url,
        profile.telemetry_baseline_seconds,
        otlp_endpoint=None,
    )
    baseline = [*baseline_before, *baseline_after]

    baseline_p95 = _percentile(baseline, 95)
    baseline_p99 = _percentile(baseline, 99)
    outage_p95 = _percentile(outage, 95)
    outage_p99 = _percentile(outage, 99)
    regression_p95 = max(0.0, outage_p95 / baseline_p95 - 1)
    regression_p99 = max(0.0, outage_p99 / baseline_p99 - 1)
    return {
        "baseline_p95_seconds": baseline_p95,
        "baseline_p99_seconds": baseline_p99,
        "baseline_before_p99_seconds": _percentile(baseline_before, 99),
        "baseline_after_p99_seconds": _percentile(baseline_after, 99),
        "outage_p95_seconds": outage_p95,
        "outage_p99_seconds": outage_p99,
        "regression_p95_ratio": regression_p95,
        "regression_p99_ratio": regression_p99,
    }


async def _run(profile: ProbeProfile, urls: dict[str, str]) -> dict[str, Any]:
    tenant_id = f"capacity-{uuid4().hex[:12]}"
    run_id = uuid4()
    await _seed_run(urls["migration"], tenant_id, run_id)

    projection_engine = create_async_engine(urls["projection"])
    reconciler = ProjectionReconciler(async_sessionmaker(projection_engine, expire_on_commit=False))
    projection_task = asyncio.create_task(reconciler.run())
    try:
        emit_seconds, _ = await _emit_events(
            urls["runtime"],
            tenant_id,
            run_id,
            start=1,
            count=profile.event_count,
            events_per_second=profile.event_rate,
        )
        event_rate = profile.event_count / emit_seconds
        await _wait_for_projection(
            urls["migration"],
            tenant_id,
            profile.event_count,
            profile.max_projection_recovery_seconds,
        )
    finally:
        reconciler.request_shutdown()
        await projection_task
        await projection_engine.dispose()

    projection_p95 = await _projection_p95(urls["migration"], tenant_id)

    recovery_started = monotonic()
    await _emit_events(
        urls["runtime"],
        tenant_id,
        run_id,
        start=profile.event_count + 1,
        count=profile.recovery_event_count,
    )
    recovery_engine = create_async_engine(urls["projection"])
    recovery_reconciler = ProjectionReconciler(async_sessionmaker(recovery_engine, expire_on_commit=False))
    try:
        await recovery_reconciler.run_once()
    finally:
        await recovery_engine.dispose()
    recovery_seconds = monotonic() - recovery_started

    total_before_sse = profile.event_count + profile.recovery_event_count
    sse_p95 = await _sse_probe(
        profile,
        urls,
        tenant_id,
        run_id,
        total_before_sse,
        total_before_sse + 1,
    )
    telemetry = await _telemetry_outage_probe(profile, urls["api"])

    measurements = {
        "runtime_event_count": profile.event_count,
        "runtime_events_per_second": event_rate,
        "projection_p95_seconds": projection_p95,
        "projection_recovery_seconds": recovery_seconds,
        "sse_connections": profile.sse_connections,
        "runtime_event_to_sse_p95_seconds": sse_p95,
        "telemetry_outage_seconds": profile.telemetry_outage_seconds,
        **telemetry,
    }
    checks = {
        "events_per_second": event_rate >= profile.event_rate,
        "projection_p95": projection_p95 <= profile.max_projection_p95_seconds,
        "projection_recovery": recovery_seconds <= profile.max_projection_recovery_seconds,
        "sse_connections": profile.sse_connections > 0,
        "sse_p95": sse_p95 <= profile.max_sse_p95_seconds,
        "max_event_bytes": profile.max_event_bytes == 65_536,
        "telemetry_p95_regression": (telemetry["regression_p95_ratio"] <= profile.max_latency_regression_ratio),
        "telemetry_p99_regression": (telemetry["regression_p99_ratio"] <= profile.max_latency_regression_ratio),
    }
    if not all(checks.values()):
        failed = next(name for name, passed in checks.items() if not passed)
        detail = json.dumps(
            {"failed": failed, "measurements": measurements},
            ensure_ascii=False,
            sort_keys=True,
        )
        raise CapacityProbeError(f"resource threshold failed: {detail}")
    return {
        "status": "PASS",
        "mode": profile.mode,
        "reference_target_claim": profile.mode == "reference_target_v1",
        "recorded_at": datetime.now(UTC).isoformat(),
        "checks": checks,
        "measurements": measurements,
    }


def _write_evidence(path: Path, result: dict[str, Any]) -> None:
    resolved = path.resolve()
    evidence_root = (ROOT / "ci-evidence").resolve()
    if evidence_root not in resolved.parents:
        raise CapacityProbeError("capacity evidence must stay under ignored ci-evidence/")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--target", action="store_true", help="run the exact 15-minute Reference Target v1")
    mode.add_argument(
        "--geometry",
        action="store_true",
        help="run target event/connection geometry with a short telemetry window",
    )
    parser.add_argument(
        "--evidence",
        type=Path,
        default=ROOT / "ci-evidence/ws4-capacity-probe.json",
    )
    args = parser.parse_args()
    try:
        profile = _load_geometry_profile() if args.geometry else _load_profile(args.target)
        urls = _database_urls()
        result = asyncio.run(_run(profile, urls))
        _write_evidence(args.evidence, result)
    except CapacityProbeError as exc:
        print(json.dumps({"status": "FAIL", "reason": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 1
    except Exception as exc:
        print(
            json.dumps(
                {"status": "FAIL", "reason": f"unexpected {type(exc).__name__}"},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
