"""WS-6: admit the domain-view runtime fact into the emit schema allowlist.

Extends ``grove_emit_runtime_events``' closed payload-schema allowlist with
``grove.runtime.domain-view-accepted.v1`` so the runtime worker can emit the
typed domain-view acceptance fact atomically with the terminal transition.
The downgrade restores the exact WS-4 body (0011 final state); grants are
unchanged because ``CREATE OR REPLACE`` preserves the ACL.
"""

# ruff: noqa: S608 -- migration interpolates only closed, source-owned SQL fragments.

from collections.abc import Sequence

from alembic import op

revision: str = "ws6_domain_view_runtime_event"
down_revision: str | None = "ws6_asset_risk_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _emit_sql(*, domain_view: bool) -> str:
    allowlist = (
        "'grove.runtime.run-lifecycle.v1',\n"
        "                       'grove.runtime.node-executed.v1',\n"
        "                       'grove.runtime.execution-audit.v1'"
    )
    if domain_view:
        allowlist += ",\n                       'grove.runtime.domain-view-accepted.v1'"
    return f"""
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
            expected_source TEXT;
        BEGIN
            IF session_user NOT IN ('grove_runtime', 'grove_api', 'grove_projection') THEN
                RAISE EXCEPTION 'authority role required for observation emit' USING ERRCODE = '42501';
            END IF;

            expected_source := CASE session_user
                WHEN 'grove_runtime' THEN 'grove.runtime_worker'
                WHEN 'grove_api' THEN 'grove.api.command'
                WHEN 'grove_projection' THEN 'grove.projection_reconciliation'
            END;

            IF NULLIF(current_setting('grove.tenant_id', true), '') IS DISTINCT FROM p_tenant_id THEN
                RAISE EXCEPTION 'observation tenant scope mismatch' USING ERRCODE = '42501';
            END IF;
            IF p_events IS NULL OR jsonb_typeof(p_events) <> 'array'
               OR jsonb_array_length(p_events) = 0 OR jsonb_array_length(p_events) > 32 THEN
                RAISE EXCEPTION 'observation event batch must contain 1..32 events' USING ERRCODE = '22023';
            END IF;

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
                BEGIN
                    evt_occurred_at := (evt->>'occurred_at')::timestamptz;
                EXCEPTION WHEN OTHERS THEN
                    RAISE EXCEPTION 'malformed observation occurred_at' USING ERRCODE = '22023';
                END;

                IF evt_event_type IS NULL OR length(evt_event_type) NOT BETWEEN 1 AND 128
                   OR evt_source IS DISTINCT FROM expected_source
                   OR evt_source_event_id IS NULL OR length(evt_source_event_id) NOT BETWEEN 1 AND 256
                   OR evt_schema_ref NOT IN (
                       {allowlist}
                   )
                   OR evt_payload IS NULL OR jsonb_typeof(evt_payload) <> 'object'
                   OR evt_occurred_at IS NULL THEN
                    RAISE EXCEPTION 'malformed or unauthorized observation descriptor' USING ERRCODE = '22023';
                END IF;
                IF session_user <> 'grove_runtime'
                   AND evt_schema_ref <> 'grove.runtime.execution-audit.v1' THEN
                    RAISE EXCEPTION 'non-runtime roles may emit audit facts only' USING ERRCODE = '42501';
                END IF;
                IF octet_length(evt_payload::text) > 65536 THEN
                    RAISE EXCEPTION 'runtime event payload exceeds 64 KiB bound' USING ERRCODE = '22023';
                END IF;

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
                ) VALUES (p_tenant_id, p_run_id, new_event_id, base_seq, evt_source);
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
    op.execute(_emit_sql(domain_view=True))


def downgrade() -> None:
    op.execute(_emit_sql(domain_view=False))
