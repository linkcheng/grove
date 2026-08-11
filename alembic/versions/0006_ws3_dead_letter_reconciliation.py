"""Add production dead-letter and expired-lease reconciliation seams.

Both functions are deliberately narrow database authorities.  They lock the
run before its command, validate the complete claim/provenance identity, and
return a stable result without exposing direct projection writes to callers.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "ws3_dead_letter_reconciliation"
down_revision: str | None = "ws3_cancel_acceptance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_DEAD_LETTER_SIGNATURE = "TEXT, UUID, UUID, BIGINT, TEXT, TEXT, TEXT, BIGINT, TIMESTAMPTZ, TEXT"
_RECONCILE_SIGNATURE = "TEXT, UUID"


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION grove_dead_letter_run_command(
            p_tenant_id TEXT,
            p_run_id UUID,
            p_command_id UUID,
            p_command_seq BIGINT,
            p_command_digest TEXT,
            p_runtime_build_hash TEXT,
            p_worker_id TEXT,
            p_execution_fence BIGINT,
            p_expected_lease_until TIMESTAMPTZ,
            p_reason_ref TEXT
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
            status TEXT
        )
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE
            command_row public.run_command%ROWTYPE;
            run_row public.agent_run%ROWTYPE;
            scoped_tenant TEXT;
            v_claim_provenance_hash TEXT;
            proof_exists BOOLEAN;
            database_now TIMESTAMPTZ;
        BEGIN
            IF session_user <> 'grove_runtime' THEN
                RAISE EXCEPTION 'runtime dead-letter role required' USING ERRCODE = '42501';
            END IF;
            scoped_tenant := NULLIF(current_setting('grove.tenant_id', true), '');
            IF scoped_tenant IS NULL OR scoped_tenant IS DISTINCT FROM p_tenant_id
               OR p_run_id IS NULL OR p_command_id IS NULL
               OR p_command_seq IS NULL OR p_command_seq < 0
               OR p_command_digest IS NULL OR p_command_digest !~ '^[0-9a-f]{64}$'
               OR p_runtime_build_hash IS NULL OR p_runtime_build_hash !~ '^[0-9a-f]{64}$'
               OR p_worker_id IS NULL OR length(p_worker_id) NOT BETWEEN 1 AND 256
               OR p_execution_fence IS NULL OR p_execution_fence < 1
               OR p_expected_lease_until IS NULL
               OR p_reason_ref IS NULL OR length(p_reason_ref) NOT BETWEEN 1 AND 512 THEN
                RAISE EXCEPTION 'invalid runtime dead-letter arguments' USING ERRCODE = '22023';
            END IF;

            database_now := clock_timestamp();
            -- The run is always locked before its command in this function, so
            -- reconciliation cannot clear a replacement lease half-way through
            -- this operation.
            SELECT * INTO run_row
              FROM public.agent_run AS r
             WHERE r.tenant_id = p_tenant_id AND r.run_id = p_run_id
             FOR UPDATE;
            IF NOT FOUND THEN
                RETURN QUERY SELECT 'stale'::TEXT, p_command_id, p_tenant_id, p_run_id,
                    p_command_seq, NULL::TEXT, NULL::TEXT, p_command_digest,
                    p_runtime_build_hash, 'unknown'::TEXT;
                RETURN;
            END IF;
            SELECT * INTO command_row
              FROM public.run_command AS c
             WHERE c.tenant_id = p_tenant_id AND c.command_id = p_command_id
             FOR UPDATE;
            IF NOT FOUND
               OR command_row.run_id <> p_run_id
               OR command_row.command_seq <> p_command_seq
               OR command_row.command_digest <> p_command_digest THEN
                RETURN QUERY SELECT 'stale'::TEXT, p_command_id, p_tenant_id, p_run_id,
                    p_command_seq, NULL::TEXT, NULL::TEXT, p_command_digest,
                    p_runtime_build_hash, 'unknown'::TEXT;
                RETURN;
            END IF;

            v_claim_provenance_hash := grove_checkpoint_claim_provenance(
                p_tenant_id, p_run_id, p_command_id, p_command_seq, p_command_digest,
                p_runtime_build_hash, p_worker_id, p_execution_fence, p_expected_lease_until
            );
            SELECT EXISTS (
                SELECT 1
                  FROM public.checkpoints AS cp
                 WHERE cp.tenant_id = p_tenant_id
                   AND cp.thread_id = p_run_id::TEXT
                   AND cp.checkpoint_id = run_row.latest_checkpoint_id
                   AND cp.claim_command_id = p_command_id
                   AND cp.claim_command_seq = p_command_seq
                   AND cp.claim_command_digest = p_command_digest
                   AND cp.claim_runtime_build_hash = p_runtime_build_hash
                   AND cp.claim_worker_id = p_worker_id
                   AND cp.claim_execution_fence = p_execution_fence
                   AND cp.claim_lease_until IS NOT DISTINCT FROM p_expected_lease_until
                   AND cp.claim_provenance_hash = v_claim_provenance_hash
            )
              AND run_row.latest_applied_command_id = p_command_id
              AND run_row.latest_applied_command_seq = p_command_seq
              AND run_row.latest_applied_command_digest = p_command_digest
              INTO proof_exists;
            IF proof_exists THEN
                RETURN QUERY SELECT 'applied'::TEXT, command_row.command_id, command_row.tenant_id,
                    command_row.run_id, command_row.command_seq, command_row.command_type,
                    command_row.command_schema_version, command_row.command_digest,
                    p_runtime_build_hash, command_row.status;
                RETURN;
            END IF;

            IF command_row.status <> 'leased'
               OR command_row.lease_owner <> p_worker_id
               OR command_row.execution_fence <> p_execution_fence
               OR command_row.lease_until IS DISTINCT FROM p_expected_lease_until
               OR run_row.lease_owner <> p_worker_id
               OR run_row.execution_fence <> p_execution_fence
               OR run_row.lease_until IS DISTINCT FROM p_expected_lease_until
               OR run_row.runtime_build_hash <> p_runtime_build_hash THEN
                RETURN QUERY SELECT 'stale'::TEXT, command_row.command_id, command_row.tenant_id,
                    command_row.run_id, command_row.command_seq, command_row.command_type,
                    command_row.command_schema_version, command_row.command_digest,
                    p_runtime_build_hash, command_row.status;
                RETURN;
            END IF;
            IF command_row.lease_until <= database_now OR run_row.lease_until <= database_now THEN
                RETURN QUERY SELECT 'expired'::TEXT, command_row.command_id, command_row.tenant_id,
                    command_row.run_id, command_row.command_seq, command_row.command_type,
                    command_row.command_schema_version, command_row.command_digest,
                    p_runtime_build_hash, command_row.status;
                RETURN;
            END IF;

            UPDATE public.run_command AS dead_letter_command
               SET status = 'dead_letter',
                   last_error_ref = p_reason_ref,
                   lease_owner = NULL,
                   lease_until = NULL,
                   execution_fence = NULL
             WHERE dead_letter_command.tenant_id = p_tenant_id
               AND dead_letter_command.command_id = p_command_id;
            UPDATE public.agent_run AS dead_letter_run
               SET lease_owner = NULL,
                   lease_until = NULL,
                   updated_at = clock_timestamp()
             WHERE dead_letter_run.tenant_id = p_tenant_id
               AND dead_letter_run.run_id = p_run_id
               AND dead_letter_run.lease_owner = p_worker_id
               AND dead_letter_run.execution_fence = p_execution_fence
               AND dead_letter_run.lease_until IS NOT DISTINCT FROM p_expected_lease_until;
            RETURN QUERY SELECT 'dead_letter'::TEXT, command_row.command_id, command_row.tenant_id,
                command_row.run_id, command_row.command_seq, command_row.command_type,
                command_row.command_schema_version, command_row.command_digest,
                p_runtime_build_hash, 'dead_letter'::TEXT;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION grove_reconcile_expired_run_command(
            p_tenant_id TEXT,
            p_run_id UUID
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
            status TEXT
        )
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE
            command_row public.run_command%ROWTYPE;
            leased_command_row public.run_command%ROWTYPE;
            run_row public.agent_run%ROWTYPE;
            scoped_tenant TEXT;
            v_claim_provenance_hash TEXT;
            physical_count BIGINT;
            physical_match_count BIGINT;
            leased_count INTEGER := 0;
            lease_shape_valid BOOLEAN := TRUE;
            current_proof BOOLEAN := FALSE;
            prior_proof BOOLEAN := FALSE;
            latest_fields_all_null BOOLEAN;
            latest_fields_complete BOOLEAN;
            database_now TIMESTAMPTZ;
        BEGIN
            IF session_user <> 'grove_projection' THEN
                RAISE EXCEPTION 'projection reconciliation role required' USING ERRCODE = '42501';
            END IF;
            scoped_tenant := NULLIF(current_setting('grove.tenant_id', true), '');
            IF scoped_tenant IS NULL OR scoped_tenant IS DISTINCT FROM p_tenant_id OR p_run_id IS NULL THEN
                RAISE EXCEPTION 'invalid projection reconciliation arguments' USING ERRCODE = '22023';
            END IF;

            -- Run-first locking is the common ordering with claim, consume and
            -- dead-letter.  The complete leased set is locked while the run
            -- lock is held; selecting only one row would let a second lease
            -- survive a requeue and would turn a partial projection into a
            -- false no-proof result.
            SELECT * INTO run_row
              FROM public.agent_run AS r
             WHERE r.tenant_id = p_tenant_id AND r.run_id = p_run_id
             FOR UPDATE;
            IF NOT FOUND THEN
                RETURN QUERY SELECT 'noop'::TEXT, NULL::UUID, p_tenant_id, p_run_id,
                    NULL::BIGINT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT;
                RETURN;
            END IF;

            FOR leased_command_row IN
                SELECT c.*
                  FROM public.run_command AS c
                 WHERE c.tenant_id = p_tenant_id
                   AND c.run_id = p_run_id
                   AND c.status = 'leased'
                 ORDER BY c.command_seq, c.command_id
                 FOR UPDATE
            LOOP
                leased_count := leased_count + 1;
                IF leased_count = 1 THEN
                    command_row := leased_command_row;
                END IF;
                IF leased_command_row.superseded_by_command_id IS NOT NULL
                   OR leased_command_row.lease_owner IS NULL
                   OR leased_command_row.execution_fence IS NULL
                   OR leased_command_row.lease_until IS NULL THEN
                    lease_shape_valid := FALSE;
                END IF;
                IF run_row.lease_owner IS DISTINCT FROM leased_command_row.lease_owner
                   OR run_row.execution_fence IS DISTINCT FROM leased_command_row.execution_fence
                   OR run_row.lease_until IS DISTINCT FROM leased_command_row.lease_until THEN
                    lease_shape_valid := FALSE;
                END IF;
            END LOOP;

            database_now := clock_timestamp();
            IF leased_count = 0 THEN
                RETURN QUERY SELECT 'noop'::TEXT, NULL::UUID, p_tenant_id, p_run_id,
                    NULL::BIGINT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT, NULL::TEXT;
                RETURN;
            END IF;

            -- More than one leased row, a superseded row, a partial lease, or
            -- any run/command lease disagreement is a manual outcome.  No
            -- row is mutated in this branch.
            -- The lifecycle/type pair is an exact protocol matrix.  The
            -- current execution slice has only start while running; cancel is
            -- the only command allowed after atomic cancel acceptance.  Do
            -- not widen this to "anything except cancel": schema-permitted
            -- resume/continue/signal rows have no worker protocol yet.
            IF leased_count <> 1
               OR NOT lease_shape_valid
               OR run_row.runtime_build_hash IS NULL
               OR run_row.runtime_build_hash !~ '^[0-9a-f]{64}$'
               OR NOT (
                    (run_row.status = 'running' AND command_row.command_type = 'start')
                    OR (run_row.status = 'cancel_requested' AND command_row.command_type = 'cancel')
               ) THEN
                RETURN QUERY SELECT 'manual'::TEXT, command_row.command_id, command_row.tenant_id,
                    command_row.run_id, command_row.command_seq, command_row.command_type,
                    command_row.command_schema_version, command_row.command_digest,
                    run_row.runtime_build_hash, command_row.status;
                RETURN;
            END IF;

            IF command_row.lease_until > database_now AND run_row.lease_until > database_now THEN
                RETURN QUERY SELECT 'noop'::TEXT, command_row.command_id, command_row.tenant_id,
                    command_row.run_id, command_row.command_seq, command_row.command_type,
                    command_row.command_schema_version, command_row.command_digest,
                    run_row.runtime_build_hash, command_row.status;
                RETURN;
            END IF;
            IF command_row.lease_until > database_now OR run_row.lease_until > database_now THEN
                RETURN QUERY SELECT 'manual'::TEXT, command_row.command_id, command_row.tenant_id,
                    command_row.run_id, command_row.command_seq, command_row.command_type,
                    command_row.command_schema_version, command_row.command_digest,
                    run_row.runtime_build_hash, command_row.status;
                RETURN;
            END IF;

            v_claim_provenance_hash := grove_checkpoint_claim_provenance(
                p_tenant_id, p_run_id, command_row.command_id, command_row.command_seq,
                command_row.command_digest, run_row.runtime_build_hash, command_row.lease_owner,
                command_row.execution_fence, command_row.lease_until
            );

            latest_fields_all_null := run_row.latest_checkpoint_id IS NULL
                AND run_row.latest_applied_command_id IS NULL
                AND run_row.latest_applied_command_seq IS NULL
                AND run_row.latest_applied_command_digest IS NULL;
            latest_fields_complete := run_row.latest_checkpoint_id IS NOT NULL
                AND run_row.latest_applied_command_id IS NOT NULL
                AND run_row.latest_applied_command_seq IS NOT NULL
                AND run_row.latest_applied_command_digest IS NOT NULL;

            -- A physical checkpoint pointer is only a proof when it is unique,
            -- carries the projected claim fields, and its provenance recomputes
            -- from those exact fields.  Counting both the candidate rows and
            -- the matching rows rejects missing, duplicate, and forged bodies.
            SELECT count(*), count(*) FILTER (WHERE
                cp.claim_command_id = command_row.command_id
                AND cp.claim_command_seq = command_row.command_seq
                AND cp.claim_command_digest = command_row.command_digest
                AND cp.claim_worker_id = command_row.lease_owner
                AND cp.claim_execution_fence = command_row.execution_fence
                AND cp.claim_lease_until IS NOT DISTINCT FROM command_row.lease_until
                AND cp.claim_runtime_build_hash = run_row.runtime_build_hash
                AND cp.claim_provenance_hash = v_claim_provenance_hash
            )
              INTO physical_count, physical_match_count
              FROM public.checkpoints AS cp
             WHERE cp.tenant_id = p_tenant_id
               AND cp.thread_id = p_run_id::TEXT
               AND cp.checkpoint_id = run_row.latest_checkpoint_id;
            current_proof := latest_fields_complete
                AND run_row.latest_applied_command_id = command_row.command_id
                AND run_row.latest_applied_command_seq = command_row.command_seq
                AND run_row.latest_applied_command_digest = command_row.command_digest
                AND physical_count = 1
                AND physical_match_count = 1;
            IF current_proof THEN
                UPDATE public.run_command AS consumed_command
                   SET status = 'consumed',
                       consumed_provenance_kind = 'claim.v1',
                       lease_owner = NULL,
                       lease_until = NULL,
                       execution_fence = NULL,
                       consumed_worker_id = command_row.lease_owner,
                       consumed_execution_fence = command_row.execution_fence,
                       consumed_lease_until = command_row.lease_until,
                       consumed_claim_provenance_hash = v_claim_provenance_hash
                 WHERE consumed_command.tenant_id = p_tenant_id
                   AND consumed_command.command_id = command_row.command_id;
                UPDATE public.agent_run AS consumed_run
                   SET lease_owner = NULL,
                       lease_until = NULL,
                       updated_at = clock_timestamp()
                 WHERE consumed_run.tenant_id = p_tenant_id
                   AND consumed_run.run_id = p_run_id
                   AND consumed_run.lease_owner = command_row.lease_owner
                   AND consumed_run.execution_fence = command_row.execution_fence
                   AND consumed_run.lease_until IS NOT DISTINCT FROM command_row.lease_until;
                RETURN QUERY SELECT 'consumed'::TEXT, command_row.command_id, command_row.tenant_id,
                    command_row.run_id, command_row.command_seq, command_row.command_type,
                    command_row.command_schema_version, command_row.command_digest,
                    run_row.runtime_build_hash, 'consumed'::TEXT;
                RETURN;
            END IF;

            IF latest_fields_complete AND run_row.latest_applied_command_seq < command_row.command_seq THEN
                -- The run lock already excludes every protocol writer, so a
                -- second lower-sequence row lock is unnecessary.  In
                -- particular, locking it after the current leased row would
                -- reverse the deterministic command order.  The exact join
                -- below is the durable lifecycle owner: projection and a
                -- self-consistent checkpoint alone can never prove a prior
                -- apply.
                SELECT count(*), count(*) FILTER (WHERE
                    cp.claim_command_id = run_row.latest_applied_command_id
                    AND cp.claim_command_seq = run_row.latest_applied_command_seq
                    AND cp.claim_command_digest = run_row.latest_applied_command_digest
                    AND cp.claim_runtime_build_hash = run_row.runtime_build_hash
                    AND prior_command.status = 'consumed'
                    AND prior_command.consumed_worker_id = cp.claim_worker_id
                    AND prior_command.consumed_execution_fence = cp.claim_execution_fence
                    AND prior_command.consumed_lease_until IS NOT DISTINCT FROM cp.claim_lease_until
                    AND prior_command.consumed_claim_provenance_hash = cp.claim_provenance_hash
                    AND cp.claim_command_id IS NOT NULL
                    AND cp.claim_command_seq IS NOT NULL
                    AND cp.claim_command_digest IS NOT NULL
                    AND cp.claim_worker_id IS NOT NULL
                    AND cp.claim_execution_fence IS NOT NULL
                    AND cp.claim_lease_until IS NOT NULL
                    AND cp.claim_provenance_hash = grove_checkpoint_claim_provenance(
                        cp.tenant_id, p_run_id, cp.claim_command_id, cp.claim_command_seq,
                        cp.claim_command_digest, cp.claim_runtime_build_hash, cp.claim_worker_id,
                        cp.claim_execution_fence, cp.claim_lease_until
                    )
                )
                INTO physical_count, physical_match_count
                  FROM public.checkpoints AS cp
             LEFT JOIN public.run_command AS prior_command
                    ON prior_command.tenant_id = cp.tenant_id
                   AND prior_command.run_id = p_run_id
                   AND prior_command.command_id = run_row.latest_applied_command_id
                   AND prior_command.command_seq = run_row.latest_applied_command_seq
                   AND prior_command.command_digest = run_row.latest_applied_command_digest
                 WHERE cp.tenant_id = p_tenant_id
                   AND cp.thread_id = p_run_id::TEXT
                   AND cp.checkpoint_id = run_row.latest_checkpoint_id;
                prior_proof := physical_count = 1 AND physical_match_count = 1;
            END IF;

            -- Pristine runs and coherent prior proofs have no proof for the
            -- expired current command, so they requeue only the exact lease.
            -- available_at, attempt_count, last_error_ref and all high-water
            -- fields remain untouched.
            IF latest_fields_all_null OR prior_proof THEN
                UPDATE public.run_command AS requeued_command
                   SET status = 'pending',
                       lease_owner = NULL,
                       lease_until = NULL,
                       execution_fence = NULL
                 WHERE requeued_command.tenant_id = p_tenant_id
                   AND requeued_command.command_id = command_row.command_id;
                UPDATE public.agent_run AS requeued_run
                   SET lease_owner = NULL,
                       lease_until = NULL,
                       updated_at = clock_timestamp()
                 WHERE requeued_run.tenant_id = p_tenant_id
                   AND requeued_run.run_id = p_run_id
                   AND requeued_run.lease_owner = command_row.lease_owner
                   AND requeued_run.execution_fence = command_row.execution_fence
                   AND requeued_run.lease_until IS NOT DISTINCT FROM command_row.lease_until;
                RETURN QUERY SELECT 'requeued'::TEXT, command_row.command_id, command_row.tenant_id,
                    command_row.run_id, command_row.command_seq, command_row.command_type,
                    command_row.command_schema_version, command_row.command_digest,
                    run_row.runtime_build_hash, 'pending'::TEXT;
                RETURN;
            END IF;

            -- Partial projections, projection-only current/higher claims,
            -- missing/forged physical rows, and sequence contradictions are
            -- manual zero-write outcomes.
            IF latest_fields_complete THEN
                RETURN QUERY SELECT 'manual'::TEXT, command_row.command_id, command_row.tenant_id,
                    command_row.run_id, command_row.command_seq, command_row.command_type,
                    command_row.command_schema_version, command_row.command_digest,
                    run_row.runtime_build_hash, command_row.status;
                RETURN;
            END IF;
            RETURN QUERY SELECT 'manual'::TEXT, command_row.command_id, command_row.tenant_id,
                command_row.run_id, command_row.command_seq, command_row.command_type,
                command_row.command_schema_version, command_row.command_digest,
                run_row.runtime_build_hash, command_row.status;
            RETURN;
        END
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION grove_dead_letter_run_command({_DEAD_LETTER_SIGNATURE}) FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION grove_reconcile_expired_run_command({_RECONCILE_SIGNATURE}) FROM PUBLIC")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grove_runtime') THEN
                GRANT EXECUTE ON FUNCTION grove_dead_letter_run_command(
                    TEXT, UUID, UUID, BIGINT, TEXT, TEXT, TEXT, BIGINT, TIMESTAMPTZ, TEXT
                ) TO grove_runtime;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grove_projection') THEN
                GRANT EXECUTE ON FUNCTION grove_reconcile_expired_run_command(TEXT, UUID)
                    TO grove_projection;
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON FUNCTION grove_dead_letter_run_command({_DEAD_LETTER_SIGNATURE}) FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION grove_reconcile_expired_run_command({_RECONCILE_SIGNATURE}) FROM PUBLIC")
    op.execute(f"DROP FUNCTION IF EXISTS grove_dead_letter_run_command({_DEAD_LETTER_SIGNATURE})")
    op.execute(f"DROP FUNCTION IF EXISTS grove_reconcile_expired_run_command({_RECONCILE_SIGNATURE})")
