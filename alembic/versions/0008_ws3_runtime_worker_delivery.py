"""Extend lifecycle predicate and add atomic finish_delivery for worker loop."""

from collections.abc import Sequence

from alembic import op

revision: str = "ws3_runtime_worker_delivery"
down_revision: str | None = "ws3_execution_authority_closure"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FINISH_SIGNATURE = "TEXT, UUID, UUID, BIGINT, TEXT, TEXT, TEXT, BIGINT, TIMESTAMPTZ, TEXT, TEXT, TEXT, JSONB"


def _lifecycle_predicate_sql() -> str:
    return """
        CREATE OR REPLACE FUNCTION grove_execution_claim_lifecycle_valid(
            p_run_status TEXT,
            p_command_type TEXT
        ) RETURNS BOOLEAN
        LANGUAGE SQL IMMUTABLE SECURITY INVOKER SET search_path = pg_catalog, public AS $$
            SELECT CASE
                WHEN p_run_status = 'running' AND p_command_type = 'start' THEN TRUE
                WHEN p_run_status = 'running' AND p_command_type = 'continue' THEN TRUE
                WHEN p_run_status = 'cancel_requested' AND p_command_type = 'cancel' THEN TRUE
                ELSE FALSE
            END
        $$
    """


def _finish_delivery_sql() -> str:
    return """
        CREATE OR REPLACE FUNCTION grove_finish_delivery(
            p_tenant_id TEXT,
            p_run_id UUID,
            p_command_id UUID,
            p_command_seq BIGINT,
            p_command_digest TEXT,
            p_runtime_build_hash TEXT,
            p_worker_id TEXT,
            p_execution_fence BIGINT,
            p_expected_lease_until TIMESTAMPTZ,
            p_outcome_kind TEXT,
            p_continue_payload_ref TEXT,
            p_continue_payload_hash TEXT,
            p_continue_payload JSONB
        ) RETURNS TABLE (
            result_code TEXT,
            command_id UUID,
            tenant_id TEXT,
            run_id UUID,
            command_seq BIGINT,
            command_type TEXT,
            command_schema_version TEXT,
            command_digest TEXT,
            runtime_build_hash TEXT,
            status TEXT,
            continue_command_id UUID,
            continue_command_seq BIGINT,
            run_revision BIGINT
        )
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE
            run_row public.agent_run%ROWTYPE;
            command_row public.run_command%ROWTYPE;
            authority_now TIMESTAMPTZ;
            claim_provenance_hash TEXT;
            proof_exists BOOLEAN;
            next_revision BIGINT;
            next_seq BIGINT;
            continue_id UUID;
        BEGIN
            IF session_user <> 'grove_runtime' THEN
                RAISE EXCEPTION 'runtime delivery role required' USING ERRCODE = '42501';
            END IF;
            IF NULLIF(current_setting('grove.tenant_id', true), '') IS DISTINCT FROM p_tenant_id THEN
                RAISE EXCEPTION 'runtime tenant scope mismatch' USING ERRCODE = '42501';
            END IF;
            IF p_outcome_kind NOT IN ('yield', 'terminal') THEN
                RAISE EXCEPTION 'invalid outcome kind' USING ERRCODE = '22023';
            END IF;
            IF p_outcome_kind = 'yield' AND (
               p_continue_payload_ref IS NULL
               OR p_continue_payload_hash IS NULL
               OR p_continue_payload IS NULL
               OR length(p_continue_payload_hash) <> 64
            ) THEN
                RAISE EXCEPTION 'yield delivery requires continue payload' USING ERRCODE = '22023';
            END IF;
            IF p_outcome_kind = 'terminal' AND (
               p_continue_payload_ref IS NOT NULL
               OR p_continue_payload_hash IS NOT NULL
               OR p_continue_payload IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'terminal delivery must not carry continue payload' USING ERRCODE = '22023';
            END IF;

            SELECT * INTO run_row FROM public.agent_run AS r
             WHERE r.tenant_id = p_tenant_id AND r.run_id = p_run_id FOR UPDATE;
            SELECT * INTO command_row FROM public.run_command AS c
             WHERE c.tenant_id = p_tenant_id AND c.command_id = p_command_id FOR UPDATE;
            authority_now := clock_timestamp();

            -- Idempotent: already consumed by this exact delivery.
            IF command_row.command_id IS NOT NULL
               AND command_row.status = 'consumed'
               AND command_row.consumed_worker_id = p_worker_id
               AND command_row.consumed_execution_fence = p_execution_fence
               AND command_row.consumed_lease_until = p_expected_lease_until
            THEN
                IF p_outcome_kind = 'yield' THEN
                    SELECT c.command_id, c.command_seq INTO continue_id, next_seq
                      FROM public.run_command AS c
                     WHERE c.tenant_id = p_tenant_id AND c.run_id = p_run_id
                       AND c.command_seq = p_command_seq + 1
                       AND c.command_type = 'continue';
                    RETURN QUERY SELECT 'consumed'::TEXT, command_row.command_id, command_row.tenant_id,
                        command_row.run_id, command_row.command_seq, command_row.command_type,
                        command_row.command_schema_version, command_row.command_digest,
                        p_runtime_build_hash, 'consumed'::TEXT,
                        continue_id, next_seq, run_row.revision;
                ELSE
                    RETURN QUERY SELECT 'consumed'::TEXT, command_row.command_id, command_row.tenant_id,
                        command_row.run_id, command_row.command_seq, command_row.command_type,
                        command_row.command_schema_version, command_row.command_digest,
                        p_runtime_build_hash, 'consumed'::TEXT,
                        NULL::UUID, NULL::BIGINT, run_row.revision;
                END IF;
                RETURN;
            END IF;

            -- Stale / expired lease check.
            IF command_row.command_id IS NULL
               OR run_row.run_id IS NULL
               OR command_row.run_id <> p_run_id
               OR command_row.command_seq <> p_command_seq
               OR command_row.command_digest <> p_command_digest
               OR command_row.status <> 'leased'
               OR command_row.superseded_by_command_id IS NOT NULL
               OR NOT public.grove_execution_claim_lifecycle_valid(
                      CASE WHEN run_row.status = 'accepted' THEN 'running' ELSE run_row.status END,
                      command_row.command_type)
               OR command_row.lease_until IS NULL OR command_row.lease_until <= authority_now
               OR run_row.lease_until IS NULL OR run_row.lease_until <= authority_now
            THEN
                RETURN QUERY SELECT 'stale'::TEXT, command_row.command_id, command_row.tenant_id,
                    command_row.run_id, command_row.command_seq, command_row.command_type,
                    command_row.command_schema_version, command_row.command_digest,
                    p_runtime_build_hash, command_row.status,
                    NULL::UUID, NULL::BIGINT, run_row.revision;
                RETURN;
            END IF;

            -- Fence identity must match exactly.
            IF command_row.lease_owner <> p_worker_id
               OR command_row.execution_fence <> p_execution_fence
               OR run_row.lease_owner <> p_worker_id
               OR run_row.execution_fence <> p_execution_fence
            THEN
                RETURN QUERY SELECT 'stale'::TEXT, command_row.command_id, command_row.tenant_id,
                    command_row.run_id, command_row.command_seq, command_row.command_type,
                    command_row.command_schema_version, command_row.command_digest,
                    p_runtime_build_hash, command_row.status,
                    NULL::UUID, NULL::BIGINT, run_row.revision;
                RETURN;
            END IF;

            -- Checkpoint proof must exist.
            claim_provenance_hash := public.grove_checkpoint_claim_provenance(
                p_tenant_id, p_run_id, p_command_id, p_command_seq,
                p_command_digest, p_runtime_build_hash, p_worker_id,
                p_execution_fence, p_expected_lease_until
            );
            SELECT EXISTS (
                SELECT 1 FROM public.checkpoints AS cp
                 WHERE cp.tenant_id = p_tenant_id
                   AND cp.thread_id = p_run_id::TEXT
                   AND cp.checkpoint_id = run_row.latest_checkpoint_id
                   AND cp.claim_command_id = p_command_id
                   AND cp.claim_command_seq = p_command_seq
                   AND cp.claim_command_digest = p_command_digest
                   AND cp.claim_runtime_build_hash = p_runtime_build_hash
            ) AND run_row.latest_applied_command_seq = p_command_seq
              AND run_row.latest_applied_command_id = p_command_id
              AND run_row.latest_applied_command_digest = p_command_digest
            INTO proof_exists;
            IF NOT proof_exists THEN
                RETURN QUERY SELECT 'no_proof'::TEXT, command_row.command_id, command_row.tenant_id,
                    command_row.run_id, command_row.command_seq, command_row.command_type,
                    command_row.command_schema_version, command_row.command_digest,
                    p_runtime_build_hash, command_row.status,
                    NULL::UUID, NULL::BIGINT, run_row.revision;
                RETURN;
            END IF;

            -- Mark current command consumed.
            UPDATE public.run_command AS consumed_command
               SET status = 'consumed',
                   consumed_provenance_kind = 'claim.v1',
                   lease_owner = NULL, lease_until = NULL, execution_fence = NULL,
                   consumed_worker_id = p_worker_id,
                   consumed_execution_fence = p_execution_fence,
                   consumed_lease_until = p_expected_lease_until,
                   consumed_claim_provenance_hash = claim_provenance_hash
             WHERE consumed_command.tenant_id = p_tenant_id
               AND consumed_command.command_id = p_command_id;

            IF p_outcome_kind = 'yield' THEN
                next_revision := run_row.revision + 1;
                next_seq := command_row.command_seq + 1;
                continue_id := gen_random_uuid();

                INSERT INTO public.command_payload (
                    tenant_id, payload_ref, payload_hash, command_schema_version,
                    sensitivity, retention, payload
                ) VALUES (
                    p_tenant_id, p_continue_payload_ref, p_continue_payload_hash,
                    'continue.v1', 'sensitive', 'run_completion', p_continue_payload
                ) ON CONFLICT ON CONSTRAINT command_payload_ref_hash_schema_uq
                  DO NOTHING;

                INSERT INTO public.run_command (
                    tenant_id, command_id, run_id, principal_id, principal_kind,
                    command_seq, command_type, command_schema_version,
                    command_digest, payload_ref, payload_hash, status
                )
                SELECT
                    p_tenant_id, continue_id, p_run_id,
                    r.principal_id, r.principal_kind,
                    next_seq, 'continue', 'continue.v1',
                    p_continue_payload_hash, p_continue_payload_ref, p_continue_payload_hash,
                    'pending'
                 FROM public.agent_run r
                 WHERE r.tenant_id = p_tenant_id AND r.run_id = p_run_id;

                UPDATE public.agent_run AS dr
                   SET lease_owner = NULL, lease_until = NULL,
                       revision = next_revision,
                       status = CASE WHEN dr.status = 'accepted' THEN 'running' ELSE dr.status END,
                       updated_at = clock_timestamp()
                 WHERE dr.tenant_id = p_tenant_id AND dr.run_id = p_run_id;

                RETURN QUERY SELECT 'consumed'::TEXT, command_row.command_id, command_row.tenant_id,
                    command_row.run_id, command_row.command_seq, command_row.command_type,
                    command_row.command_schema_version, command_row.command_digest,
                    p_runtime_build_hash, 'consumed'::TEXT,
                    continue_id, next_seq, next_revision;
            ELSE
                UPDATE public.agent_run AS dr
                   SET lease_owner = NULL, lease_until = NULL,
                       status = 'succeeded', updated_at = clock_timestamp()
                 WHERE dr.tenant_id = p_tenant_id AND dr.run_id = p_run_id;

                RETURN QUERY SELECT 'consumed'::TEXT, command_row.command_id, command_row.tenant_id,
                    command_row.run_id, command_row.command_seq, command_row.command_type,
                    command_row.command_schema_version, command_row.command_digest,
                    p_runtime_build_hash, 'consumed'::TEXT,
                    NULL::UUID, NULL::BIGINT, run_row.revision;
            END IF;
        END
        $$
    """


def upgrade() -> None:
    op.execute(_lifecycle_predicate_sql())
    op.execute(_finish_delivery_sql())
    op.execute("REVOKE ALL ON FUNCTION grove_execution_claim_lifecycle_valid(TEXT, TEXT) FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION grove_finish_delivery({_FINISH_SIGNATURE}) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION grove_finish_delivery({_FINISH_SIGNATURE}) TO grove_runtime")


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON FUNCTION grove_finish_delivery({_FINISH_SIGNATURE}) FROM PUBLIC, grove_runtime")
    op.execute(f"DROP FUNCTION IF EXISTS grove_finish_delivery({_FINISH_SIGNATURE})")
    op.execute("""
        CREATE OR REPLACE FUNCTION grove_execution_claim_lifecycle_valid(
            p_run_status TEXT,
            p_command_type TEXT
        ) RETURNS BOOLEAN
        LANGUAGE SQL IMMUTABLE SECURITY INVOKER SET search_path = pg_catalog, public AS $$
            SELECT CASE
                WHEN p_run_status = 'running' AND p_command_type = 'start' THEN TRUE
                WHEN p_run_status = 'cancel_requested' AND p_command_type = 'cancel' THEN TRUE
                ELSE FALSE
            END
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION grove_execution_claim_lifecycle_valid(TEXT, TEXT) FROM PUBLIC")
