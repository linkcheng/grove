"""Claim-bound LangGraph checkpoint persistence.

The public object deliberately implements the pinned ``BaseCheckpointSaver``
contract.  It composes the pinned PostgreSQL saver for serializer, read SQL,
config specs, and version allocation, while owning the physical write rows so
the surrounding transaction installs one immutable claim context consumed by
the database authority triggers on all three tables.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_serializable_checkpoint_metadata,
)
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.base import SerializerProtocol
from langgraph.checkpoint.serde.types import _DeltaSnapshot
from psycopg.types.json import Jsonb

from app.core.telemetry import record_operation
from app.execution.contracts import ExecutionClaim, _strict_claim
from app.observation.emitter import emit_runtime_events_psycopg
from app.observation.facts import RUNTIME_WORKER_SOURCE, build_execution_audit_emit_request

_RESERVED_METADATA_KEYS = frozenset(
    {
        "tenant_id",
        "run_id",
        "applied_command_id",
        "applied_command_seq",
        "applied_command_digest",
        "runtime_build_hash",
        "worker_id",
        "execution_fence",
        "lease_until",
        "claim_fingerprint",
        "claim_provenance_hash",
        "checkpoint_ref",
        "checkpoint_hash",
        "checkpoint_content_hash",
    }
)
_KNOWN_INTERNAL_CONFIG_KEYS = frozenset(
    {
        "__pregel_send",
        "__pregel_read",
        "__pregel_call",
        "__pregel_checkpointer",
        "__pregel_stream",
        "__pregel_cache",
        "__pregel_resuming",
        "__pregel_replay_state",
        "__pregel_task_id",
        "__pregel_node_finished",
        "__pregel_timed_attempt_observer",
        "__pregel_scratchpad",
        "__pregel_runner_submit",
        "__pregel_durability",
        "__pregel_runtime",
        "__pregel_resume_map",
        "__pregel_stream_messages_v2",
        "__pregel_node_error",
    }
)

# The upstream 3.1.1 read seam and serializer remain pinned.  Its write seam
# is deliberately not reused: blobs use ``DO NOTHING`` and checkpoints omit
# parent/type from the update, so neither can prove a physical retry or conflict.
_PINNED_WRITE_SEAM = "langgraph-checkpoint-postgres==3.1.1"
_CHECKPOINT_MARKER_PREFIX = b"grove.checkpoint.marker.v1\x00"
_CHECKPOINT_EMPTY_MARKER = _CHECKPOINT_MARKER_PREFIX + b"absent"
_FENCED_UPSERT_CHECKPOINT_BLOBS_SQL = (
    "INSERT INTO public.checkpoint_blobs "
    "(thread_id, checkpoint_ns, channel, version, type, blob) "
    "VALUES (%s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (thread_id, checkpoint_ns, channel, version) DO UPDATE "
    "SET type = EXCLUDED.type, blob = EXCLUDED.blob"
)
_FENCED_UPSERT_CHECKPOINTS_SQL = (
    "INSERT INTO public.checkpoints "
    "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id) DO UPDATE SET "
    "parent_checkpoint_id = EXCLUDED.parent_checkpoint_id, "
    "type = EXCLUDED.type, checkpoint = EXCLUDED.checkpoint, metadata = EXCLUDED.metadata"
)
_FENCED_UPSERT_CHECKPOINT_WRITES_SQL = (
    "INSERT INTO public.checkpoint_writes "
    "(thread_id, checkpoint_ns, checkpoint_id, task_id, task_path, idx, channel, type, blob) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id, task_id, idx) DO UPDATE SET "
    "task_path = EXCLUDED.task_path, channel = EXCLUDED.channel, "
    "type = EXCLUDED.type, blob = EXCLUDED.blob"
)


class FencedPostgresSaver(BaseCheckpointSaver[str]):
    """A standard async checkpointer bound to one exact execution claim.

    ``ExecutionClaim`` is validated once at construction and never replaced.
    Every config is required to identify that same run; the database trigger
    then re-checks the complete claim against the current lease before any
    checkpoint, blob, or pending-write row can be visible.
    """

    def __init__(
        self,
        connection: Any,
        claim: ExecutionClaim,
        *,
        serde: SerializerProtocol | None = None,
    ) -> None:
        trusted_claim, _ = _strict_claim(claim)
        super().__init__(serde=serde)
        self._connection = connection
        self._claim = trusted_claim
        self._saver = AsyncPostgresSaver(connection, serde=self.serde)
        self._operation_lock = asyncio.Lock()

    @property
    def claim(self) -> ExecutionClaim:
        """Return the immutable, strictly validated claim bound to this saver."""

        return self._claim

    @property
    def config_specs(self) -> list[Any]:
        return self._saver.config_specs

    def get_next_version(self, current: str | None, channel: None) -> str:
        return self._saver.get_next_version(current, channel)

    async def setup(self) -> None:
        """Reject runtime DDL; the pinned migration owns the checkpoint schema."""

        raise RuntimeError("pinned migration owns checkpoint schema; saver.setup() is forbidden")

    @staticmethod
    def _text(value: object, *, label: str, maximum: int, allow_empty: bool = False) -> str:
        if type(value) is not str or (not allow_empty and not value) or len(value) > maximum:
            raise ValueError(f"{label} must be a non-empty exact string")
        return value

    def _config(
        self,
        config: RunnableConfig,
        *,
        require_checkpoint_id: bool = False,
    ) -> RunnableConfig:
        if not isinstance(config, Mapping):
            raise ValueError("checkpoint config must be a mapping")
        configurable = config.get("configurable")
        if not isinstance(configurable, Mapping):
            raise ValueError("checkpoint config must contain configurable mapping")
        allowed = {"thread_id", "checkpoint_ns", "checkpoint_id", "checkpoint_map"}
        if any(
            type(key) is not str or (key not in allowed and key not in _KNOWN_INTERNAL_CONFIG_KEYS)
            for key in configurable
        ):
            raise ValueError("checkpoint config contains an unknown configurable field")
        caller_metadata = config.get("metadata")
        if caller_metadata is not None:
            if not isinstance(caller_metadata, Mapping):
                raise ValueError("checkpoint config metadata must be a mapping")
            if any(type(key) is not str for key in caller_metadata):
                raise ValueError("checkpoint config metadata keys must be strings")
            reserved = _RESERVED_METADATA_KEYS.intersection(caller_metadata)
            if reserved:
                raise ValueError(f"reserved checkpoint metadata cannot be overridden: {sorted(reserved)!r}")
        thread_id = configurable.get("thread_id")
        expected_thread_id = str(self._claim.run_id)
        if type(thread_id) is not str or thread_id != expected_thread_id:
            raise ValueError("thread_id must equal claim.run_id")
        checkpoint_ns = self._text(
            configurable.get("checkpoint_ns", ""),
            label="checkpoint_ns",
            maximum=256,
            allow_empty=True,
        )
        checkpoint_id = configurable.get("checkpoint_id")
        if checkpoint_id is not None:
            checkpoint_id = self._text(checkpoint_id, label="checkpoint_id", maximum=256)
        if require_checkpoint_id and checkpoint_id is None:
            raise ValueError("checkpoint_id is required for checkpoint writes")
        # Keep caller metadata in the validated config so LangGraph's pinned
        # metadata merge can retain it while explicit metadata remains the
        # precedence layer.
        safe_metadata = dict(caller_metadata or {})
        return cast(
            RunnableConfig,
            {
                "configurable": {
                    "thread_id": expected_thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    **({"checkpoint_id": checkpoint_id} if checkpoint_id is not None else {}),
                },
                "metadata": safe_metadata,
            },
        )

    @staticmethod
    def _metadata(metadata: CheckpointMetadata) -> CheckpointMetadata:
        if not isinstance(metadata, Mapping):
            raise ValueError("checkpoint metadata must be a mapping")
        user_metadata = dict(metadata)
        if any(type(key) is not str for key in user_metadata):
            raise ValueError("checkpoint metadata keys must be strings")
        reserved = _RESERVED_METADATA_KEYS.intersection(user_metadata)
        if reserved:
            raise ValueError(f"reserved checkpoint metadata cannot be overridden: {sorted(reserved)!r}")
        return cast(CheckpointMetadata, user_metadata)

    @staticmethod
    def _requires_blob(value: object) -> bool:
        """Mirror the pinned saver primitive split before it opens a connection."""

        return value is not None and not isinstance(value, (str, int, float, bool))

    @classmethod
    def _validate_versions_and_blob_channels(
        cls,
        checkpoint: Mapping[str, Any],
        new_versions: Mapping[str, object],
    ) -> tuple[str, ...]:
        """Validate the pinned ``new_versions`` subset and return blob references.

        ``AsyncPostgresSaver`` treats ``new_versions`` as the channels changed by
        this step, not as a copy of every channel version in the checkpoint.  A
        non-primitive channel still references a blob at its checkpoint version;
        the database trigger checks that the row exists when the checkpoint is
        committed, including the unchanged-blob reuse case.
        """

        raw_versions = checkpoint.get("channel_versions")
        if not isinstance(raw_versions, Mapping):
            raise ValueError("checkpoint.channel_versions must be a mapping")
        channel_versions: dict[str, str | int | float] = {}
        for channel, version in raw_versions.items():
            if type(channel) is not str or not channel:
                raise ValueError("checkpoint channel version keys must be non-empty exact strings")
            if (
                type(version) not in {str, int, float}
                or (type(version) is str and not version)
                or (type(version) is float and not math.isfinite(version))
            ):
                raise ValueError("checkpoint channel versions must be exact strings or finite numeric values")
            channel_versions[channel] = cast(str | int | float, version)

        for channel, version in new_versions.items():
            if type(channel) is not str or not channel:
                raise ValueError("new_versions channel keys must be non-empty exact strings")
            if (
                type(version) not in {str, int, float}
                or (type(version) is str and not version)
                or (type(version) is float and not math.isfinite(version))
            ):
                raise ValueError("new_versions values must be exact strings or finite numeric values")
            if channel not in channel_versions:
                raise ValueError("new_versions channel must exist in checkpoint.channel_versions")
            if type(channel_versions[channel]) is not type(version) or channel_versions[channel] != version:
                raise ValueError("new_versions version must equal checkpoint.channel_versions")

        raw_values = checkpoint.get("channel_values")
        if not isinstance(raw_values, Mapping):
            raise ValueError("checkpoint.channel_values must be a mapping")
        blob_channels: list[str] = []
        for channel, value in raw_values.items():
            if type(channel) is not str or not channel:
                raise ValueError("checkpoint channel value keys must be non-empty exact strings")
            if cls._requires_blob(value):
                if channel not in channel_versions:
                    raise ValueError("non-primitive channel must have a checkpoint version")
                blob_channels.append(channel)
        return tuple(sorted(blob_channels))

    def _prepare_checkpoint(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> tuple[RunnableConfig, Checkpoint, CheckpointMetadata, ChannelVersions]:
        safe_config = self._config(config)
        if not isinstance(checkpoint, Mapping):
            raise ValueError("checkpoint must be a mapping")
        checkpoint_data = dict(checkpoint)
        checkpoint_id = self._text(checkpoint_data.get("id"), label="checkpoint.id", maximum=256)
        del checkpoint_id  # Validation is intentionally before any connection access.
        safe_metadata = self._metadata(metadata)
        if not isinstance(new_versions, Mapping):
            raise ValueError("checkpoint channel versions must be a mapping")
        self._validate_versions_and_blob_channels(checkpoint_data, new_versions)
        return (
            safe_config,
            cast(Checkpoint, checkpoint_data),
            safe_metadata,
            cast(ChannelVersions, dict(new_versions)),
        )

    def _materialize_checkpoint(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
    ) -> tuple[
        RunnableConfig,
        list[tuple[str, str, str, str, str, bytes | None]],
        tuple[str, str, str, str | None, str | None, Jsonb, Jsonb],
        tuple[str, ...],
    ]:
        """Build the complete pinned physical checkpoint write set.

        The upstream 3.1.1 adapter only materializes channels named by
        ``new_versions``.  This private materializer instead serializes every
        non-primitive value present in the current checkpoint, while retaining
        the upstream inline/``_DeltaSnapshot`` split and metadata conversion.
        The returned rows are consumed only by the two custom write paths.
        """

        configurable = cast(Mapping[str, Any], config["configurable"])
        thread_id = cast(str, configurable["thread_id"])
        checkpoint_ns = cast(str, configurable["checkpoint_ns"])
        parent_checkpoint_id = cast(str | None, configurable.get("checkpoint_id"))
        checkpoint_id = self._text(checkpoint["id"], label="checkpoint.id", maximum=256)
        raw_values = cast(Mapping[str, Any], checkpoint["channel_values"])
        raw_versions = cast(Mapping[str, str | int | float], checkpoint["channel_versions"])
        checkpoint_copy = dict(checkpoint)
        copied_values = dict(raw_values)
        checkpoint_copy["channel_values"] = copied_values
        blob_rows: list[tuple[str, str, str, str, str, bytes | None]] = []
        blob_channels: list[str] = []
        for channel, version in sorted(raw_versions.items()):
            value = raw_values.get(channel)
            if isinstance(value, _DeltaSnapshot):
                blob_value = value
                copied_values.pop(channel, None)
                copied_values[channel] = True
                type_tag, blob = self.serde.dumps_typed(blob_value)
                blob_type = type_tag
            elif value is None or isinstance(value, (str, int, float, bool)):
                type_tag, blob = self.serde.dumps_typed(value)
                blob_type = "empty"
                blob = _CHECKPOINT_MARKER_PREFIX + type_tag.encode("utf-8") + b"\x00" + blob
            else:
                blob_value = value
                copied_values.pop(channel, None)
                type_tag, blob = self.serde.dumps_typed(blob_value)
                blob_type = type_tag
            if channel not in raw_values:
                blob_type = "empty"
                blob = _CHECKPOINT_EMPTY_MARKER
            blob_rows.append(
                (
                    thread_id,
                    checkpoint_ns,
                    channel,
                    str(version),
                    blob_type,
                    blob,
                )
            )
            blob_channels.append(channel)

        serializable_metadata = get_serializable_checkpoint_metadata(config, metadata)
        checkpoint_row = (
            thread_id,
            checkpoint_ns,
            checkpoint_id,
            parent_checkpoint_id,
            cast(str | None, checkpoint.get("type")),
            Jsonb(checkpoint_copy),
            Jsonb(serializable_metadata),
        )
        next_config = cast(
            RunnableConfig,
            {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": checkpoint_id,
                }
            },
        )
        return next_config, blob_rows, checkpoint_row, tuple(sorted(blob_channels))

    def _prepare_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> tuple[RunnableConfig, Sequence[tuple[str, Any]], str, str]:
        safe_config = self._config(config, require_checkpoint_id=True)
        if type(task_id) is not str or not task_id or len(task_id) > 256:
            raise ValueError("task_id must be a non-empty exact string")
        if type(task_path) is not str or len(task_path) > 512:
            raise ValueError("task_path must be an exact string")
        if not isinstance(writes, Sequence) or isinstance(writes, (str, bytes, bytearray)):
            raise ValueError("writes must be a sequence")
        normalized: list[tuple[str, Any]] = []
        for write in writes:
            if not isinstance(write, tuple) or len(write) != 2 or type(write[0]) is not str or not write[0]:
                raise ValueError("writes must contain (channel, value) tuples")
            normalized.append((write[0], write[1]))
        return safe_config, normalized, task_id, task_path

    def _materialize_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str,
    ) -> list[tuple[str, str, str, str, str, int, str, str, bytes]]:
        """Build pinned serializer and index rows for regular/control writes."""

        configurable = cast(Mapping[str, Any], config["configurable"])
        thread_id = cast(str, configurable["thread_id"])
        checkpoint_ns = cast(str, configurable["checkpoint_ns"])
        checkpoint_id = cast(str, configurable["checkpoint_id"])
        rows: list[tuple[str, str, str, str, str, int, str, str, bytes]] = []
        for offset, (channel, value) in enumerate(writes):
            type_tag, blob = self.serde.dumps_typed(value)
            rows.append(
                (
                    thread_id,
                    checkpoint_ns,
                    checkpoint_id,
                    task_id,
                    task_path,
                    WRITES_IDX_MAP.get(channel, offset),
                    channel,
                    type_tag,
                    blob,
                )
            )
        return rows

    async def _set_blob_context(self, channels: Sequence[str]) -> None:
        payload = json.dumps(list(channels), separators=(",", ":"))
        async with self._connection.cursor() as cursor:
            await cursor.execute("SELECT set_config(%s, %s, true)", ("grove.checkpoint.blob_channels", payload))

    async def _set_claim_context(self) -> None:
        claim = self._claim
        values = {
            "grove.tenant_id": claim.tenant_id,
            "grove.checkpoint.command_id": str(claim.command_id),
            "grove.checkpoint.run_id": str(claim.run_id),
            "grove.checkpoint.command_seq": str(claim.command_seq),
            "grove.checkpoint.command_digest": claim.command_digest,
            "grove.checkpoint.runtime_build_hash": claim.runtime_build_hash,
            "grove.checkpoint.worker_id": claim.worker_id,
            "grove.checkpoint.execution_fence": str(claim.execution_fence),
            "grove.checkpoint.lease_until": claim.lease_until.isoformat(),
        }
        async with self._connection.cursor() as cursor:
            for name, value in values.items():
                await cursor.execute("SELECT set_config(%s, %s, true)", (name, value))

    @asynccontextmanager
    async def _scope(self) -> AsyncIterator[None]:
        async with self._operation_lock:
            async with self._connection.transaction():
                await self._set_claim_context()
                yield

    def _read_config(self, config: RunnableConfig | None) -> RunnableConfig:
        if config is None:
            return cast(
                RunnableConfig,
                {"configurable": {"thread_id": str(self._claim.run_id), "checkpoint_ns": ""}},
            )
        return self._config(config)

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        started = perf_counter()
        safe_config, checkpoint_data, safe_metadata, versions = self._prepare_checkpoint(
            config, checkpoint, metadata, new_versions
        )
        next_config, blob_rows, checkpoint_row, blob_channels = self._materialize_checkpoint(
            safe_config, checkpoint_data, safe_metadata
        )
        async with self._scope():
            await self._set_blob_context(blob_channels)
            async with self._connection.cursor() as cursor:
                if blob_rows:
                    await cursor.executemany(_FENCED_UPSERT_CHECKPOINT_BLOBS_SQL, blob_rows)
                await cursor.execute(_FENCED_UPSERT_CHECKPOINTS_SQL, checkpoint_row)
            checkpoint_id = checkpoint_data["id"]
            await emit_runtime_events_psycopg(
                self._connection,
                tenant_id=self._claim.tenant_id,
                run_id=self._claim.run_id,
                causation_id=self._claim.command_id,
                events=[
                    build_execution_audit_emit_request(
                        source=RUNTIME_WORKER_SOURCE,
                        run_id=self._claim.run_id,
                        command_id=self._claim.command_id,
                        command_seq=self._claim.command_seq,
                        command_type="start" if self._claim.command_seq == 0 else "continue",
                        action="checkpoint_applied",
                        result_code="applied",
                        occurred_at=datetime.now(UTC),
                        transition_key=(f"{self._claim.command_id}:{self._claim.execution_fence}:{checkpoint_id}"),
                    )
                ],
            )
        record_operation(
            "checkpoint.apply",
            duration_ms=float((perf_counter() - started) * 1000),
            role="runtime_worker",
            operation="checkpoint",
            outcome="ok",
        )
        return next_config

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        safe_config, normalized, safe_task_id, safe_task_path = self._prepare_writes(config, writes, task_id, task_path)
        rows = self._materialize_writes(safe_config, normalized, safe_task_id, safe_task_path)
        async with self._scope():
            if rows:
                async with self._connection.cursor() as cursor:
                    await cursor.executemany(_FENCED_UPSERT_CHECKPOINT_WRITES_SQL, rows)

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        safe_config = self._read_config(config)
        async with self._scope():
            return await self._saver.aget_tuple(safe_config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        safe_config = self._read_config(config)
        safe_before = self._read_config(before) if before is not None else None
        if filter is not None and not isinstance(filter, dict):
            raise ValueError("checkpoint filter must be a mapping")
        async with self._scope():
            async for item in self._saver.alist(safe_config, filter=filter, before=safe_before, limit=limit):
                yield item

    async def adelete_thread(self, thread_id: str) -> None:
        del thread_id
        raise RuntimeError("runtime checkpoint deletion is forbidden")
