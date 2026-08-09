from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from app.execution import ExecutionClaim, PostgresExecutionDriver
from app.execution import checkpoint as checkpoint_module
from app.execution.checkpoint import FencedPostgresSaver
from app.execution.contracts import RunCommandReceipt, RunStateConflict, StaleExecutionFence
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver, Checkpoint, CheckpointMetadata, CheckpointTuple

BUILD = "b" * 64


def _claim() -> ExecutionClaim:
    return ExecutionClaim(
        command_id=uuid4(),
        tenant_id="tenant-a",
        run_id=uuid4(),
        command_seq=0,
        command_digest="a" * 64,
        runtime_build_hash=BUILD,
        worker_id="worker-a",
        execution_fence=1,
        lease_until=datetime.now(UTC) + timedelta(seconds=30),
    )


def _checkpoint(claim: ExecutionClaim) -> dict[str, object]:
    return {
        "v": 1,
        "id": "checkpoint-1",
        "ts": "2026-08-06T00:00:00Z",
        "channel_values": {"state": {"answer": 42}},
        "channel_versions": {"state": "00000000000000000000000000000001.0.0"},
        "versions_seen": {},
    }


def _config(claim: ExecutionClaim) -> dict[str, object]:
    return {
        "configurable": {
            "thread_id": str(claim.run_id),
            "checkpoint_ns": "",
        }
    }


@pytest.mark.asyncio
async def test_fenced_saver_is_standard_base_checkpointer_and_claim_bound() -> None:
    claim = _claim()
    saver = FencedPostgresSaver(_Connection(), claim)
    assert isinstance(saver, BaseCheckpointSaver)
    assert saver.serde is not None
    assert saver.config_specs == []
    assert saver.get_next_version(None, None) is not None
    assert list(inspect.signature(saver.aput).parameters) == [
        "config",
        "checkpoint",
        "metadata",
        "new_versions",
    ]
    assert list(inspect.signature(saver.aput_writes).parameters) == [
        "config",
        "writes",
        "task_id",
        "task_path",
    ]
    with pytest.raises(ValueError, match="thread_id must equal claim.run_id"):
        await saver.aget_tuple(cast(RunnableConfig, {"configurable": {"thread_id": str(uuid4())}}))


@pytest.mark.asyncio
async def test_fenced_saver_delete_is_fail_closed_without_database_access() -> None:
    connection = _Connection()
    saver = FencedPostgresSaver(connection, _claim())
    with pytest.raises(RuntimeError, match="delet"):
        await saver.adelete_thread("run")
    assert connection.transaction_count == 0


class _Cursor:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, statement: str, parameters: object) -> None:
        self.connection.statements.append((statement, parameters))

    async def executemany(self, statement: str, parameters: object) -> None:
        self.connection.statements.append((statement, parameters))


class _Transaction:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _Transaction:
        self.connection.transaction_count += 1
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.connection.transaction_exits += 1


class _Connection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, object]] = []
        self.transaction_count = 0
        self.transaction_exits = 0

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def transaction(self) -> _Transaction:
        return _Transaction(self)


@pytest.mark.asyncio
async def test_fenced_saver_never_runs_pinned_setup() -> None:
    saver = FencedPostgresSaver(object(), _claim())
    with pytest.raises(RuntimeError, match="pinned migration"):
        await saver.setup()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        {"tenant_id": "forged"},
        {"run_id": str(uuid4())},
        {"applied_command_seq": 99},
        {"checkpoint_hash": "f" * 64},
    ],
)
async def test_fenced_saver_rejects_reserved_metadata_before_connection_access(
    metadata: dict[str, object],
) -> None:
    claim = _claim()
    saver = FencedPostgresSaver(object(), claim)
    with pytest.raises(ValueError, match="reserved checkpoint metadata"):
        await saver.aput(
            cast(RunnableConfig, _config(claim)),
            cast(Checkpoint, _checkpoint(claim)),
            cast(CheckpointMetadata, metadata),
            {"state": "1"},
        )


@pytest.mark.asyncio
async def test_fenced_saver_requires_thread_id_to_equal_run_id() -> None:
    claim = _claim()
    saver = FencedPostgresSaver(object(), claim)
    config = _config(claim)
    config["configurable"] = {"thread_id": str(uuid4()), "checkpoint_ns": ""}
    with pytest.raises(ValueError, match="thread_id must equal claim.run_id"):
        await saver.aput(
            cast(RunnableConfig, config),
            cast(Checkpoint, _checkpoint(claim)),
            {},
            {"state": "1"},
        )


@pytest.mark.asyncio
async def test_fenced_saver_uses_closed_langgraph_internal_config_discriminator() -> None:
    claim = _claim()
    saver = FencedPostgresSaver(object(), claim)
    with pytest.raises(ValueError, match="unknown configurable field"):
        await saver.aget_tuple(
            cast(
                RunnableConfig,
                {
                    "configurable": {
                        "thread_id": str(claim.run_id),
                        "checkpoint_ns": "",
                        "__pregel_forged": object(),
                    }
                },
            )
        )


def test_consume_contract_exports_stable_business_errors() -> None:
    assert issubclass(RunStateConflict, ValueError)
    assert issubclass(StaleExecutionFence, ValueError)
    assert RunCommandReceipt.model_fields["status"].annotation is not None
    assert PostgresExecutionDriver is not None


@pytest.mark.asyncio
async def test_fenced_saver_writes_and_reads_on_the_same_scoped_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    claim = _claim()
    connection = _Connection()
    saver = FencedPostgresSaver(connection, claim)
    returned_config: RunnableConfig = cast(
        RunnableConfig,
        {
            "configurable": {
                "thread_id": str(claim.run_id),
                "checkpoint_ns": "",
                "checkpoint_id": "checkpoint-1",
            }
        },
    )

    async def forbidden_aput(*_args: Any, **_kwargs: Any) -> RunnableConfig:
        raise AssertionError("custom checkpoint writer must not delegate to upstream aput")

    expected_tuple = cast(CheckpointTuple, object())

    async def fake_aget_tuple(_config: RunnableConfig) -> CheckpointTuple:
        return expected_tuple

    async def fake_alist(
        _config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Any:
        del filter, before, limit
        yield expected_tuple

    monkeypatch.setattr(saver._saver, "aput", forbidden_aput)
    monkeypatch.setattr(saver._saver, "aget_tuple", fake_aget_tuple)
    monkeypatch.setattr(saver._saver, "alist", fake_alist)
    result = await saver.aput(
        cast(RunnableConfig, _config(claim)),
        cast(Checkpoint, _checkpoint(claim)),
        cast(CheckpointMetadata, {"node": "unit"}),
        {"state": cast(dict[str, str], _checkpoint(claim)["channel_versions"])["state"]},
    )
    assert result == returned_config
    assert connection.transaction_count == connection.transaction_exits == 1
    parameters = [parameters for _statement, parameters in connection.statements]
    assert parameters[:10] == [
        ("grove.tenant_id", claim.tenant_id),
        ("grove.checkpoint.command_id", str(claim.command_id)),
        ("grove.checkpoint.run_id", str(claim.run_id)),
        ("grove.checkpoint.command_seq", str(claim.command_seq)),
        ("grove.checkpoint.command_digest", claim.command_digest),
        ("grove.checkpoint.runtime_build_hash", claim.runtime_build_hash),
        ("grove.checkpoint.worker_id", claim.worker_id),
        ("grove.checkpoint.execution_fence", str(claim.execution_fence)),
        ("grove.checkpoint.lease_until", claim.lease_until.isoformat()),
        ("grove.checkpoint.blob_channels", '["state"]'),
    ]
    assert len(parameters) == 12
    assert "ON CONFLICT (thread_id, checkpoint_ns, channel, version) DO UPDATE" in connection.statements[10][0]
    assert "ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id) DO UPDATE" in connection.statements[11][0]
    assert await saver.aget_tuple(returned_config) is expected_tuple
    assert [item async for item in saver.alist(returned_config)] == [expected_tuple]
    assert connection.transaction_count == connection.transaction_exits == 3


@pytest.mark.asyncio
async def test_fenced_saver_write_methods_never_delegate_to_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    claim = _claim()
    connection = _Connection()
    saver = FencedPostgresSaver(connection, claim)

    async def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("custom writer must own physical writes")

    monkeypatch.setattr(saver._saver, "aput", forbidden)
    monkeypatch.setattr(saver._saver, "aput_writes", forbidden)
    checkpoint = cast(Checkpoint, _checkpoint(claim))
    await saver.aput(
        cast(RunnableConfig, _config(claim)),
        checkpoint,
        cast(CheckpointMetadata, {}),
        cast(Any, checkpoint["channel_versions"]),
    )
    await saver.aput_writes(
        cast(
            RunnableConfig,
            {
                "configurable": {
                    "thread_id": str(claim.run_id),
                    "checkpoint_ns": "",
                    "checkpoint_id": checkpoint["id"],
                }
            },
        ),
        [("state", {"answer": 42})],
        "task",
        "path",
    )


def test_fenced_write_sql_is_schema_qualified() -> None:
    sql = (
        checkpoint_module._FENCED_UPSERT_CHECKPOINT_BLOBS_SQL,
        checkpoint_module._FENCED_UPSERT_CHECKPOINTS_SQL,
        checkpoint_module._FENCED_UPSERT_CHECKPOINT_WRITES_SQL,
    )
    assert "public.checkpoint_blobs" in sql[0]
    assert "public.checkpoints" in sql[1]
    assert "public.checkpoint_writes" in sql[2]
    assert all("INSERT INTO checkpoint_" not in statement for statement in sql)


@pytest.mark.asyncio
async def test_checkpoint_config_metadata_is_retained_after_validation() -> None:
    claim = _claim()
    saver = FencedPostgresSaver(_Connection(), claim)
    safe = saver._config(
        cast(
            RunnableConfig,
            {
                "configurable": {"thread_id": str(claim.run_id), "checkpoint_ns": ""},
                "metadata": {"caller_tag": "from-config", "shared": "config"},
            },
        )
    )
    assert safe["metadata"] == {"caller_tag": "from-config", "shared": "config"}


@pytest.mark.asyncio
async def test_fenced_saver_reads_require_constructor_run_context() -> None:
    saver = FencedPostgresSaver(_Connection(), _claim())
    config = cast(RunnableConfig, _config(_claim()))
    with pytest.raises(ValueError, match="thread_id must equal claim.run_id"):
        await saver.aget_tuple(config)
    with pytest.raises(ValueError, match="thread_id must equal claim.run_id"):
        _ = [item async for item in saver.alist(config)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "missing-configurable",
        "unknown-configurable",
        "bad-checkpoint",
        "bad-checkpoint-id",
        "bad-metadata",
        "bad-config-metadata",
        "bad-metadata-key",
        "bad-versions",
    ],
)
async def test_fenced_saver_rejects_malformed_input_before_connection_access(case: str) -> None:
    claim = _claim()
    connection = _Connection()
    saver = FencedPostgresSaver(connection, claim)
    config: dict[str, object] = _config(claim)
    checkpoint: object = _checkpoint(claim)
    metadata: object = {}
    versions: object = {"state": "1"}
    if case == "missing-configurable":
        config = {}
    elif case == "unknown-configurable":
        config["configurable"] = {"thread_id": str(claim.run_id), "unknown": "x"}
    elif case == "bad-checkpoint":
        checkpoint = []
    elif case == "bad-checkpoint-id":
        checkpoint = {**_checkpoint(claim), "id": 1}
    elif case == "bad-metadata":
        metadata = []
    elif case == "bad-config-metadata":
        config["metadata"] = []
    elif case == "bad-metadata-key":
        metadata = {1: "bad"}
    else:
        versions = []
    with pytest.raises(ValueError):
        await saver.aput(
            cast(RunnableConfig, config),
            cast(Checkpoint, checkpoint),
            cast(CheckpointMetadata, metadata),
            cast(Any, versions),
        )
    assert connection.transaction_count == 0
    assert connection.statements == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("versions", "message"),
    [
        ({"orphan": "1"}, "new_versions channel must exist"),
        ({"state": "2"}, "new_versions version must equal"),
    ],
)
async def test_fenced_saver_rejects_orphan_or_mismatched_new_versions_before_connection_access(
    versions: dict[str, str], message: str
) -> None:
    connection = _Connection()
    saver = FencedPostgresSaver(connection, _claim())
    with pytest.raises(ValueError, match=message):
        await saver.aput(
            cast(RunnableConfig, _config(saver.claim)),
            cast(Checkpoint, _checkpoint(saver.claim)),
            cast(CheckpointMetadata, {}),
            cast(Any, versions),
        )
    assert connection.transaction_count == 0
    assert connection.statements == []


@pytest.mark.asyncio
async def test_fenced_saver_config_and_write_boundaries_are_explicit() -> None:
    claim = _claim()
    saver = FencedPostgresSaver(_Connection(), claim)
    assert saver.claim == claim
    assert saver._read_config(None)["configurable"]["thread_id"] == str(claim.run_id)

    valid = cast(RunnableConfig, _config(claim))
    writes_config = cast(
        RunnableConfig,
        {"configurable": {**cast(dict[str, object], valid["configurable"]), "checkpoint_id": "checkpoint"}},
    )
    assert saver._prepare_writes(writes_config, [("state", 1)], "task", "path")[2:] == ("task", "path")
    invalid_configs: list[object] = [
        object(),
        {"configurable": object()},
        {"configurable": {1: "bad"}},
        {"configurable": valid["configurable"], "metadata": object()},
        {"configurable": valid["configurable"], "metadata": {1: "bad"}},
        {"configurable": valid["configurable"], "metadata": {"run_id": "forged"}},
    ]
    for invalid in invalid_configs:
        with pytest.raises(ValueError):
            saver._config(cast(RunnableConfig, invalid))

    invalid_writes: list[tuple[object, object, object, object]] = [
        (valid, [], "", ""),
        (valid, [], "task", "x" * 513),
        (valid, "not-writes", "task", ""),
        (valid, [("state",)], "task", ""),
    ]
    for _config_value, writes, task_id, task_path in invalid_writes:
        with pytest.raises(ValueError):
            saver._prepare_writes(
                writes_config,
                cast(Any, writes),
                cast(str, task_id),
                cast(str, task_path),
            )
