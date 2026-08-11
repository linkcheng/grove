"""WS-4 Observation Slice: runtime event/outbox, projection read model, watermark.

Adds the observation-side persistence boundary on top of the WS-3 durable
execution tables.  The authority transaction (runtime role) emits versioned
RuntimeEvent facts and outbox rows atomically via ``grove_emit_runtime_events``;
the projection role consumes the outbox into a rebuildable UI projection read
model.  No WS-3 state-machine transition function is modified: the emit is an
additive observation step executed in the same transaction as delivery.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "ws4_observation_slice"
down_revision: str | None = "ws3_runtime_worker_delivery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EMIT_SIGNATURE = "TEXT, UUID, UUID, TEXT, UUID, TEXT, JSONB"

_OBSERVATION_TABLES = (
    "runtime_event",
    "runtime_event_outbox",
    "ui_projection_event",
    "projection_watermark",
    "runtime_event_dead_letter",
)


def _runtime_event_sql() -> str:
    return """
        CREATE TABLE runtime_event (
            event_id        UUID PRIMARY KEY,
            run_seq         BIGINT NOT NULL,
            tenant_id       TEXT NOT NULL,
            run_id          UUID NOT NULL,
            orchestration_id UUID NOT NULL,
            correlation_id  TEXT NOT NULL,
            causation_id    UUID,
            trace_id        TEXT,
            source          TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            event_type      TEXT NOT NULL,
            event_schema_version TEXT NOT NULL,
            payload_schema_ref TEXT NOT NULL,
            payload         JSONB NOT NULL,
            occurred_at     TIMESTAMPTZ NOT NULL,
            recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT runtime_event_seq_ck CHECK (run_seq >= 1),
            CONSTRAINT runtime_event_schema_version_ck
                CHECK (event_schema_version IN ('v1')),
            CONSTRAINT runtime_event_run_seq_uq UNIQUE (run_id, run_seq),
            CONSTRAINT runtime_event_source_dedup_uq
                UNIQUE (tenant_id, source, source_event_id)
        )
    """


def _outbox_sql() -> str:
    return """
        CREATE TABLE runtime_event_outbox (
            outbox_id   BIGSERIAL PRIMARY KEY,
            tenant_id   TEXT NOT NULL,
            run_id      UUID NOT NULL,
            event_id    UUID NOT NULL,
            run_seq     BIGINT NOT NULL,
            source      TEXT NOT NULL,
            enqueued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            relayed_at  TIMESTAMPTZ,
            CONSTRAINT runtime_event_outbox_seq_ck CHECK (run_seq >= 1),
            CONSTRAINT runtime_event_outbox_event_uq UNIQUE (event_id),
            CONSTRAINT runtime_event_outbox_event_fk
                FOREIGN KEY (event_id) REFERENCES public.runtime_event (event_id)
        )
    """


def _ui_projection_sql() -> str:
    return """
        CREATE TABLE ui_projection_event (
            projection_event_id BIGSERIAL PRIMARY KEY,
            tenant_id      TEXT NOT NULL,
            target_kind    TEXT NOT NULL,
            target_ref     UUID NOT NULL,
            event_id       UUID NOT NULL,
            projection_seq BIGINT NOT NULL,
            contract_version TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            causation_id   UUID,
            trace_id       TEXT,
            payload_schema_ref TEXT NOT NULL,
            payload        JSONB NOT NULL,
            source_refs    JSONB NOT NULL,
            projected_at   TIMESTAMPTZ NOT NULL,
            recorded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ui_projection_seq_ck CHECK (projection_seq >= 1),
            CONSTRAINT ui_projection_target_kind_ck
                CHECK (target_kind IN ('run', 'orchestration')),
            CONSTRAINT ui_projection_target_seq_uq
                UNIQUE (tenant_id, target_kind, target_ref, projection_seq),
            CONSTRAINT ui_projection_event_uq UNIQUE (tenant_id, event_id)
        )
    """


def _watermark_sql() -> str:
    return """
        CREATE TABLE projection_watermark (
            tenant_id      TEXT NOT NULL,
            source         TEXT NOT NULL,
            last_outbox_id BIGINT NOT NULL DEFAULT 0,
            last_run_seq   BIGINT NOT NULL DEFAULT 0,
            event_count    BIGINT NOT NULL DEFAULT 0,
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT projection_watermark_pkey PRIMARY KEY (tenant_id, source),
            CONSTRAINT projection_watermark_nonneg_ck
                CHECK (last_outbox_id >= 0 AND last_run_seq >= 0 AND event_count >= 0)
        )
    """


def _dead_letter_sql() -> str:
    return """
        CREATE TABLE runtime_event_dead_letter (
            dead_letter_id BIGSERIAL PRIMARY KEY,
            tenant_id      TEXT NOT NULL,
            run_id         UUID NOT NULL,
            event_id       UUID NOT NULL,
            run_seq        BIGINT NOT NULL,
            source         TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            event_type     TEXT NOT NULL,
            payload_schema_ref TEXT NOT NULL,
            payload        JSONB NOT NULL,
            reason         TEXT NOT NULL,
            dead_lettered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT runtime_event_dead_letter_seq_ck CHECK (run_seq >= 1),
            CONSTRAINT runtime_event_dead_letter_event_uq UNIQUE (tenant_id, event_id)
        )
    """


def _emit_function_sql() -> str:
    return """
        CREATE OR REPLACE FUNCTION grove_emit_runtime_events(
            p_tenant_id TEXT,
            p_run_id UUID,
            p_orchestration_id UUID,
            p_correlation_id TEXT,
            p_causation_id UUID,
            p_trace_id TEXT,
            p_events JSONB
        ) RETURNS TABLE (event_id UUID, run_seq BIGINT, source_event_id TEXT)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE
            run_exists BOOLEAN;
            base_seq BIGINT;
            evt JSONB;
            evt_event_type TEXT;
            evt_source TEXT;
            evt_source_event_id TEXT;
            evt_schema_ref TEXT;
            evt_payload JSONB;
            evt_occurred_at TIMESTAMPTZ;
            new_event_id UUID;
            emitted_count INTEGER := 0;
            already_exists BOOLEAN;
        BEGIN
            IF session_user <> 'grove_runtime' THEN
                RAISE EXCEPTION 'runtime role required for observation emit' USING ERRCODE = '42501';
            END IF;
            IF NULLIF(current_setting('grove.tenant_id', true), '') IS DISTINCT FROM p_tenant_id THEN
                RAISE EXCEPTION 'runtime tenant scope mismatch' USING ERRCODE = '42501';
            END IF;
            IF p_events IS NULL OR jsonb_typeof(p_events) <> 'array'
               OR jsonb_array_length(p_events) = 0 THEN
                RETURN;
            END IF;

            -- Commit-order run_seq allocation requires the authoritative run lock.
            -- When called from the delivery transaction the row is already locked;
            -- the same-transaction re-lock is a no-op and never deadlocks.
            SELECT TRUE INTO run_exists FROM public.agent_run AS r
             WHERE r.tenant_id = p_tenant_id AND r.run_id = p_run_id FOR UPDATE;
            IF NOT run_exists THEN
                RAISE EXCEPTION 'run not found for observation emit' USING ERRCODE = 'P0002';
            END IF;

            base_seq := COALESCE((
                SELECT MAX(e.run_seq) FROM public.runtime_event e
                 WHERE e.tenant_id = p_tenant_id AND e.run_id = p_run_id
            ), 0);

            FOR evt IN SELECT * FROM jsonb_array_elements(p_events) LOOP
                evt_event_type := evt->>'event_type';
                evt_source := evt->>'source';
                evt_source_event_id := evt->>'source_event_id';
                evt_schema_ref := evt->>'payload_schema_ref';
                evt_payload := evt->'payload';
                evt_occurred_at := (evt->>'occurred_at')::timestamptz;

                IF evt_event_type IS NULL OR evt_source IS NULL
                   OR evt_source_event_id IS NULL OR evt_schema_ref IS NULL
                   OR evt_payload IS NULL OR evt_occurred_at IS NULL THEN
                    RAISE EXCEPTION 'malformed observation event descriptor' USING ERRCODE = '22023';
                END IF;
                -- Resource boundary: a single event payload must stay bounded.
                IF octet_length(evt_payload::text) > 65536 THEN
                    RAISE EXCEPTION 'runtime event payload exceeds 64 KiB bound' USING ERRCODE = '22023';
                END IF;

                -- Idempotent: a retried delivery must not duplicate the stream.
                SELECT EXISTS (
                    SELECT 1 FROM public.runtime_event e
                     WHERE e.tenant_id = p_tenant_id AND e.source = evt_source
                       AND e.source_event_id = evt_source_event_id
                ) INTO already_exists;
                IF already_exists THEN
                    CONTINUE;
                END IF;

                base_seq := base_seq + 1;
                new_event_id := gen_random_uuid();

                INSERT INTO public.runtime_event (
                    event_id, run_seq, tenant_id, run_id, orchestration_id,
                    correlation_id, causation_id, trace_id, source, source_event_id,
                    event_type, event_schema_version, payload_schema_ref, payload, occurred_at
                ) VALUES (
                    new_event_id, base_seq, p_tenant_id, p_run_id, p_orchestration_id,
                    p_correlation_id, p_causation_id, p_trace_id, evt_source, evt_source_event_id,
                    evt_event_type, 'v1', evt_schema_ref, evt_payload, evt_occurred_at
                );

                INSERT INTO public.runtime_event_outbox (
                    tenant_id, run_id, event_id, run_seq, source
                ) VALUES (
                    p_tenant_id, p_run_id, new_event_id, base_seq, evt_source
                );

                emitted_count := emitted_count + 1;
                RETURN QUERY SELECT new_event_id, base_seq, evt_source_event_id;
            END LOOP;

            IF emitted_count > 0 THEN
                PERFORM pg_notify('grove_runtime_event', p_tenant_id);
            END IF;
        END
        $$
    """


def upgrade() -> None:
    op.execute(_runtime_event_sql())
    op.execute(
        "CREATE INDEX runtime_event_tenant_run_seq_idx "
        "ON runtime_event (tenant_id, run_id, run_seq)"
    )
    op.execute(
        "CREATE INDEX runtime_event_source_idx "
        "ON runtime_event (tenant_id, source, source_event_id)"
    )

    op.execute(_outbox_sql())
    op.execute(
        "CREATE INDEX runtime_event_outbox_pending_idx "
        "ON runtime_event_outbox (tenant_id, outbox_id) "
        "WHERE relayed_at IS NULL"
    )

    op.execute(_ui_projection_sql())
    op.execute(
        "CREATE INDEX ui_projection_event_target_seq_idx "
        "ON ui_projection_event (tenant_id, target_kind, target_ref, projection_seq)"
    )

    op.execute(_watermark_sql())
    op.execute(_dead_letter_sql())

    for table in _OBSERVATION_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant_isolation ON {table} "
            "USING (tenant_id = grove_active_tenant()) "
            "WITH CHECK (tenant_id = grove_active_tenant())"
        )

    op.execute(_emit_function_sql())
    op.execute(f"REVOKE ALL ON FUNCTION grove_emit_runtime_events({_EMIT_SIGNATURE}) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION grove_emit_runtime_events({_EMIT_SIGNATURE}) TO grove_runtime")

    # The runtime role never reads/writes observation tables directly: it only
    # executes the SECURITY DEFINER emit function.  The projection role owns the
    # read model; the API role reads the safe views.
    op.execute(
        "GRANT SELECT ON runtime_event, runtime_event_outbox, ui_projection_event, "
        "projection_watermark, runtime_event_dead_letter TO grove_api"
    )
    op.execute(
        "GRANT SELECT ON runtime_event, runtime_event_outbox TO grove_projection"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON ui_projection_event, projection_watermark, "
        "runtime_event_dead_letter TO grove_projection"
    )
    op.execute(
        "GRANT UPDATE (relayed_at) ON runtime_event_outbox TO grove_projection"
    )
    op.execute(
        "GRANT USAGE, SELECT ON SEQUENCE runtime_event_outbox_outbox_id_seq, "
        "ui_projection_event_projection_event_id_seq, "
        "runtime_event_dead_letter_dead_letter_id_seq TO grove_projection"
    )


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON FUNCTION grove_emit_runtime_events({_EMIT_SIGNATURE}) FROM PUBLIC, grove_runtime")
    op.execute(f"DROP FUNCTION IF EXISTS grove_emit_runtime_events({_EMIT_SIGNATURE})")
    for table in _OBSERVATION_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
