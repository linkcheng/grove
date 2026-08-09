"""Close the WS-3 execution authority around one lifecycle and one clock.

This revision starts a new execution-authority design cycle.  The previous
dead-letter/reconciliation bodies are retained only under private, non-
executable implementation names so the downgrade can restore the exact 0006
catalog.  New public function names are deterministic wrappers: they acquire the protocol locks, sample
the post-lock authority time, validate the shared lifecycle predicate, and
delegate only after the legacy physical operation is safe to run.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "ws3_execution_authority_closure"
down_revision: str | None = "ws3_dead_letter_reconciliation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CLAIM_SIGNATURE = "TEXT, TEXT, TEXT, DOUBLE PRECISION"
_HEARTBEAT_SIGNATURE = "TEXT, UUID, UUID, BIGINT, TEXT, TEXT, TEXT, BIGINT, TIMESTAMPTZ, DOUBLE PRECISION"
_CONSUME_SIGNATURE = "TEXT, UUID, UUID, BIGINT, TEXT, TEXT, TEXT, BIGINT, TIMESTAMPTZ"
_DEAD_LETTER_SIGNATURE = "TEXT, UUID, UUID, BIGINT, TEXT, TEXT, TEXT, BIGINT, TIMESTAMPTZ, TEXT"
_RECONCILE_SIGNATURE = "TEXT, UUID"


def _lifecycle_predicate_sql() -> str:
    return """
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
    """


def _claim_wrapper_sql() -> str:
    return """
        CREATE OR REPLACE FUNCTION grove_claim_run_command(
            p_tenant_id TEXT,
            p_worker_id TEXT,
            p_runtime_build_hash TEXT,
            p_lease_seconds DOUBLE PRECISION
        ) RETURNS TABLE (
            result_code TEXT,
            command_id UUID,
            run_id UUID,
            command_seq BIGINT,
            command_digest TEXT,
            runtime_build_hash TEXT,
            execution_fence BIGINT,
            lease_until TIMESTAMPTZ
        )
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE
            candidate RECORD;
            run_row public.agent_run%ROWTYPE;
            command_row public.run_command%ROWTYPE;
            discovery_now TIMESTAMPTZ;
            authority_now TIMESTAMPTZ;
            next_fence BIGINT;
            next_lease_until TIMESTAMPTZ;
            run_update_count INTEGER;
            command_update_count INTEGER;
        BEGIN
            IF session_user <> 'grove_runtime' THEN
                RAISE EXCEPTION 'runtime claim role required' USING ERRCODE = '42501';
            END IF;
            IF NULLIF(current_setting('grove.tenant_id', true), '') IS DISTINCT FROM p_tenant_id THEN
                RAISE EXCEPTION 'runtime tenant scope mismatch' USING ERRCODE = '42501';
            END IF;
            IF p_worker_id IS NULL OR length(p_worker_id) NOT BETWEEN 1 AND 256
               OR p_runtime_build_hash IS NULL OR p_runtime_build_hash !~ '^[0-9a-f]{64}$'
               OR p_lease_seconds IS NULL OR p_lease_seconds <= 0 OR p_lease_seconds > 90
               OR p_lease_seconds = 'Infinity'::float8 OR p_lease_seconds = '-Infinity'::float8
               OR p_lease_seconds = 'NaN'::float8 THEN
                RAISE EXCEPTION 'invalid runtime claim arguments' USING ERRCODE = '22023';
            END IF;

            discovery_now := clock_timestamp();
            -- Discovery is deliberately separate from authority.  The
            -- run row is locked here with SKIP LOCKED, so a blocked first run
            -- cannot starve a later eligible run.  The command is selected
            -- and locked only after that run lock succeeds.
            FOR candidate IN
                SELECT r.tenant_id AS candidate_run_tenant_id,
                       r.run_id AS candidate_run_id,
                       r.status AS candidate_run_status,
                       r.runtime_build_hash AS candidate_run_runtime_build_hash,
                       r.lease_owner AS candidate_run_lease_owner,
                       r.lease_until AS candidate_run_lease_until,
                       r.execution_fence AS candidate_run_execution_fence,
                       c.tenant_id AS candidate_command_tenant_id,
                       c.command_id AS candidate_command_id,
                       c.run_id AS candidate_command_run_id,
                       c.command_seq AS candidate_command_seq,
                       c.command_digest AS candidate_command_digest,
                       c.command_type AS candidate_command_type,
                       c.command_schema_version AS candidate_command_schema_version,
                       c.status AS candidate_command_status,
                       c.available_at AS candidate_command_available_at,
                       c.lease_owner AS candidate_command_lease_owner,
                       c.lease_until AS candidate_command_lease_until,
                       c.execution_fence AS candidate_command_execution_fence,
                       c.superseded_by_command_id AS candidate_command_superseded_by_command_id
                  FROM public.agent_run AS r
                  JOIN LATERAL (
                       SELECT c.*
                         FROM public.run_command AS c
                        WHERE c.tenant_id = r.tenant_id
                          AND c.run_id = r.run_id
                          AND c.superseded_by_command_id IS NULL
                          AND c.available_at <= discovery_now
                          AND (c.status = 'pending' OR (c.status = 'leased' AND c.lease_until <= discovery_now))
                          AND (
                               (r.status = 'accepted' AND c.command_type = 'start')
                               OR public.grove_execution_claim_lifecycle_valid(r.status, c.command_type)
                          )
                        ORDER BY (c.command_type = 'cancel') DESC, c.available_at, c.created_at, c.command_id
                        LIMIT 1
                  ) AS c ON TRUE
                 WHERE r.tenant_id = p_tenant_id
                   AND r.runtime_build_hash = p_runtime_build_hash
                   AND r.status NOT IN ('succeeded', 'failed', 'cancelled')
                   AND (r.lease_until IS NULL OR r.lease_until <= discovery_now)
                 ORDER BY (
                     EXISTS (
                         SELECT 1 FROM public.run_command AS cancel_command
                          WHERE cancel_command.tenant_id = r.tenant_id
                            AND cancel_command.run_id = r.run_id
                            AND cancel_command.command_type = 'cancel'
                            AND cancel_command.superseded_by_command_id IS NULL
                            AND cancel_command.available_at <= discovery_now
                            AND (cancel_command.status = 'pending'
                                 OR (cancel_command.status = 'leased' AND cancel_command.lease_until <= discovery_now))
                     )
                 ) DESC, r.created_at, r.run_id
                 FOR UPDATE OF r SKIP LOCKED
            LOOP
                -- Keep the existing non-blocking SKIP LOCKED contract for
                -- both run and command rows.  This is a discovery candidate;
                -- all authoritative checks happen after both locks succeed.
                SELECT * INTO run_row
                  FROM public.agent_run AS r
                 WHERE r.tenant_id = candidate.candidate_run_tenant_id
                   AND r.run_id = candidate.candidate_run_id
                 FOR UPDATE SKIP LOCKED;
                IF NOT FOUND THEN
                    CONTINUE;
                END IF;
                SELECT * INTO command_row
                  FROM public.run_command AS c
                 WHERE c.tenant_id = candidate.candidate_command_tenant_id
                   AND c.command_id = candidate.candidate_command_id
                 FOR UPDATE SKIP LOCKED;
                IF NOT FOUND THEN
                    -- An abnormal command-only lock must be a no-write
                    -- result; stop while the run lock is still local to this
                    -- transaction so no lock inversion can be introduced.
                    RETURN;
                END IF;

                authority_now := clock_timestamp();
                -- The discovery snapshot is advisory only.  Every field that
                -- can affect authority is compared after both locks, so a
                -- command supersede/rebind or a run identity/build/fence
                -- change cannot be claimed through a stale candidate.
                IF run_row.tenant_id IS DISTINCT FROM candidate.candidate_run_tenant_id
                   OR run_row.run_id IS DISTINCT FROM candidate.candidate_run_id
                   OR run_row.status IS DISTINCT FROM candidate.candidate_run_status
                   OR run_row.runtime_build_hash IS DISTINCT FROM candidate.candidate_run_runtime_build_hash
                   OR run_row.lease_owner IS DISTINCT FROM candidate.candidate_run_lease_owner
                   OR run_row.lease_until IS DISTINCT FROM candidate.candidate_run_lease_until
                   OR run_row.execution_fence IS DISTINCT FROM candidate.candidate_run_execution_fence
                   OR command_row.tenant_id IS DISTINCT FROM candidate.candidate_command_tenant_id
                   OR command_row.command_id IS DISTINCT FROM candidate.candidate_command_id
                   OR command_row.run_id IS DISTINCT FROM candidate.candidate_command_run_id
                   OR command_row.command_seq IS DISTINCT FROM candidate.candidate_command_seq
                   OR command_row.command_digest IS DISTINCT FROM candidate.candidate_command_digest
                   OR command_row.command_type IS DISTINCT FROM candidate.candidate_command_type
                   OR command_row.command_schema_version IS DISTINCT FROM candidate.candidate_command_schema_version
                   OR command_row.status IS DISTINCT FROM candidate.candidate_command_status
                   OR command_row.available_at IS DISTINCT FROM candidate.candidate_command_available_at
                   OR command_row.lease_owner IS DISTINCT FROM candidate.candidate_command_lease_owner
                   OR command_row.lease_until IS DISTINCT FROM candidate.candidate_command_lease_until
                   OR command_row.execution_fence IS DISTINCT FROM candidate.candidate_command_execution_fence
                   OR command_row.superseded_by_command_id IS DISTINCT FROM
                      candidate.candidate_command_superseded_by_command_id
                   OR command_row.superseded_by_command_id IS NOT NULL
                   OR command_row.available_at > authority_now
                   OR (command_row.status <> 'pending'
                       AND (command_row.status <> 'leased' OR command_row.lease_until > authority_now))
                   OR run_row.lease_until IS NOT NULL AND run_row.lease_until > authority_now
                   OR run_row.runtime_build_hash <> p_runtime_build_hash THEN
                    CONTINUE;
                END IF;
                -- Accepted/start is only the durable entry state.  Compute
                -- the exact post-transition pair locally, then apply the
                -- shared predicate; accepted, waiting and unknown pairs
                -- never pass as an active lifecycle.
                IF run_row.status = 'accepted' AND command_row.command_type = 'start' THEN
                    run_row.status := 'running';
                END IF;
                IF NOT public.grove_execution_claim_lifecycle_valid(run_row.status, command_row.command_type) THEN
                    CONTINUE;
                END IF;

                IF run_row.execution_fence >= 9223372036854775807 THEN
                    RETURN QUERY SELECT 'fence_exhausted'::TEXT, command_row.command_id, run_row.run_id,
                        command_row.command_seq, command_row.command_digest, run_row.runtime_build_hash,
                        run_row.execution_fence, NULL::TIMESTAMPTZ;
                    RETURN;
                END IF;
                next_fence := run_row.execution_fence + 1;
                next_lease_until := authority_now + make_interval(secs => p_lease_seconds);
                -- Both writes are a single CAS unit.  A zero-row command CAS
                -- rolls back the preceding run update in this subtransaction
                -- and returns the stable not-ready/no-row result.  Other
                -- database/trigger errors are not swallowed.
                BEGIN
                    UPDATE public.agent_run AS claimed_run
                       SET execution_fence = next_fence,
                           lease_owner = p_worker_id,
                           lease_until = next_lease_until,
                           status = CASE WHEN status = 'accepted' THEN 'running' ELSE status END,
                           updated_at = clock_timestamp()
                     WHERE claimed_run.tenant_id = candidate.candidate_run_tenant_id
                       AND claimed_run.run_id = candidate.candidate_run_id
                       AND claimed_run.status = candidate.candidate_run_status
                       AND claimed_run.runtime_build_hash = candidate.candidate_run_runtime_build_hash
                       AND claimed_run.lease_owner IS NOT DISTINCT FROM candidate.candidate_run_lease_owner
                       AND claimed_run.lease_until IS NOT DISTINCT FROM candidate.candidate_run_lease_until
                       AND claimed_run.execution_fence = candidate.candidate_run_execution_fence
                       AND (claimed_run.lease_until IS NULL OR claimed_run.lease_until <= authority_now);
                    GET DIAGNOSTICS run_update_count = ROW_COUNT;
                    IF run_update_count = 0 THEN
                        RETURN;
                    END IF;
                    IF run_update_count <> 1 THEN
                        RAISE EXCEPTION 'claim run CAS affected unexpected rows' USING ERRCODE = 'XX000';
                    END IF;
                    UPDATE public.run_command AS claimed_command
                       SET status = 'leased', lease_owner = p_worker_id,
                           lease_until = next_lease_until, execution_fence = next_fence,
                           attempt_count = attempt_count + 1
                     WHERE claimed_command.tenant_id = candidate.candidate_command_tenant_id
                       AND claimed_command.command_id = candidate.candidate_command_id
                       AND claimed_command.run_id = candidate.candidate_command_run_id
                       AND claimed_command.command_seq = candidate.candidate_command_seq
                       AND claimed_command.command_digest = candidate.candidate_command_digest
                       AND claimed_command.command_type = candidate.candidate_command_type
                       AND claimed_command.command_schema_version = candidate.candidate_command_schema_version
                       AND claimed_command.status = candidate.candidate_command_status
                       AND claimed_command.available_at IS NOT DISTINCT FROM candidate.candidate_command_available_at
                       AND claimed_command.lease_owner IS NOT DISTINCT FROM candidate.candidate_command_lease_owner
                       AND claimed_command.lease_until IS NOT DISTINCT FROM candidate.candidate_command_lease_until
                       AND claimed_command.execution_fence IS NOT DISTINCT FROM
                           candidate.candidate_command_execution_fence
                       AND claimed_command.superseded_by_command_id IS NULL
                       AND claimed_command.available_at <= authority_now
                       AND (claimed_command.status = 'pending'
                            OR (claimed_command.status = 'leased' AND claimed_command.lease_until <= authority_now));
                    GET DIAGNOSTICS command_update_count = ROW_COUNT;
                    IF command_update_count = 0 THEN
                        -- GV001 is a Grove-private five-character SQLSTATE,
                        -- reserved solely for this local CAS-miss
                        -- subtransaction.  PostgreSQL's real 40001,
                        -- deadlock, lock-timeout, trigger, and program
                        -- failures must propagate to the outer transaction.
                        RAISE EXCEPTION 'claim command CAS changed after run lock' USING ERRCODE = 'GV001';
                    END IF;
                    IF command_update_count <> 1 THEN
                        RAISE EXCEPTION 'claim command CAS affected unexpected rows' USING ERRCODE = 'XX000';
                    END IF;
                EXCEPTION
                    -- Catch only the private CAS miss.  Never catch
                    -- serialization_failure (40001) or WHEN OTHERS: those
                    -- errors are durable transaction failures, not a
                    -- stable not-ready result.
                    WHEN SQLSTATE 'GV001' THEN
                        RETURN;
                END;
                RETURN QUERY SELECT 'claimed'::TEXT, command_row.command_id, run_row.run_id,
                    command_row.command_seq, command_row.command_digest, run_row.runtime_build_hash,
                    next_fence, next_lease_until;
                RETURN;
            END LOOP;

            IF EXISTS (
                SELECT 1
                  FROM public.run_command AS c
                  JOIN public.agent_run AS r
                    ON r.tenant_id = c.tenant_id AND r.run_id = c.run_id
                 WHERE c.tenant_id = p_tenant_id
                   AND r.runtime_build_hash = p_runtime_build_hash
                   AND r.status NOT IN ('succeeded', 'failed', 'cancelled')
                   AND c.superseded_by_command_id IS NULL
                   AND c.status IN ('pending', 'leased')
            ) THEN
                RETURN;
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM public.run_command AS c
                  JOIN public.agent_run AS r
                    ON r.tenant_id = c.tenant_id AND r.run_id = c.run_id
                 WHERE c.tenant_id = p_tenant_id
                   AND r.runtime_build_hash <> p_runtime_build_hash
                   AND r.status NOT IN ('succeeded', 'failed', 'cancelled')
                   AND c.superseded_by_command_id IS NULL
                   AND c.status IN ('pending', 'leased')
            ) THEN
                RETURN QUERY SELECT 'version_unavailable'::TEXT, NULL::UUID, NULL::UUID,
                    NULL::BIGINT, NULL::TEXT, NULL::TEXT, NULL::BIGINT, NULL::TIMESTAMPTZ;
            END IF;
            RETURN;
        END
        $$
    """


def _heartbeat_wrapper_sql() -> str:
    return """
        CREATE OR REPLACE FUNCTION grove_heartbeat_run_command(
            p_tenant_id TEXT,
            p_run_id UUID,
            p_command_id UUID,
            p_command_seq BIGINT,
            p_command_digest TEXT,
            p_runtime_build_hash TEXT,
            p_worker_id TEXT,
            p_execution_fence BIGINT,
            p_expected_lease_until TIMESTAMPTZ,
            p_lease_seconds DOUBLE PRECISION
        ) RETURNS TIMESTAMPTZ
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE
            run_row public.agent_run%ROWTYPE;
            command_row public.run_command%ROWTYPE;
            authority_now TIMESTAMPTZ;
        BEGIN
            IF session_user <> 'grove_runtime' THEN
                RAISE EXCEPTION 'runtime heartbeat role required' USING ERRCODE = '42501';
            END IF;
            IF NULLIF(current_setting('grove.tenant_id', true), '') IS DISTINCT FROM p_tenant_id THEN
                RAISE EXCEPTION 'runtime tenant scope mismatch' USING ERRCODE = '42501';
            END IF;
            SELECT * INTO run_row
              FROM public.agent_run AS r
             WHERE r.tenant_id = p_tenant_id AND r.run_id = p_run_id
             FOR UPDATE;
            SELECT * INTO command_row
              FROM public.run_command AS c
             WHERE c.tenant_id = p_tenant_id AND c.command_id = p_command_id
             FOR UPDATE;
            authority_now := clock_timestamp();
            IF run_row.run_id IS NULL OR command_row.command_id IS NULL
               OR command_row.run_id <> p_run_id
               OR command_row.command_seq <> p_command_seq
               OR command_row.command_digest <> p_command_digest
               OR command_row.status <> 'leased'
               OR command_row.lease_owner <> p_worker_id
               OR command_row.execution_fence <> p_execution_fence
               OR command_row.lease_until IS DISTINCT FROM p_expected_lease_until
               OR run_row.lease_owner <> p_worker_id
               OR run_row.runtime_build_hash <> p_runtime_build_hash
               OR run_row.execution_fence <> p_execution_fence
               OR run_row.lease_until IS DISTINCT FROM p_expected_lease_until
               OR run_row.lease_until <= authority_now
               OR command_row.lease_until <= authority_now
               OR NOT public.grove_execution_claim_lifecycle_valid(run_row.status, command_row.command_type) THEN
                RETURN NULL;
            END IF;
            RETURN public.grove_heartbeat_run_command_internal(
                p_tenant_id, p_run_id, p_command_id, p_command_seq, p_command_digest,
                p_runtime_build_hash, p_worker_id, p_execution_fence,
                p_expected_lease_until, p_lease_seconds
            );
        END
        $$
    """


def _consume_wrapper_sql() -> str:
    return """
        CREATE OR REPLACE FUNCTION grove_consume_run_command(
            p_tenant_id TEXT,
            p_run_id UUID,
            p_command_id UUID,
            p_command_seq BIGINT,
            p_command_digest TEXT,
            p_runtime_build_hash TEXT,
            p_worker_id TEXT,
            p_execution_fence BIGINT,
            p_expected_lease_until TIMESTAMPTZ
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
            run_row public.agent_run%ROWTYPE;
            command_row public.run_command%ROWTYPE;
            authority_now TIMESTAMPTZ;
        BEGIN
            IF session_user <> 'grove_runtime' THEN
                RAISE EXCEPTION 'runtime consume role required' USING ERRCODE = '42501';
            END IF;
            IF NULLIF(current_setting('grove.tenant_id', true), '') IS DISTINCT FROM p_tenant_id THEN
                RAISE EXCEPTION 'runtime tenant scope mismatch' USING ERRCODE = '42501';
            END IF;
            SELECT * INTO run_row FROM public.agent_run AS r
             WHERE r.tenant_id = p_tenant_id AND r.run_id = p_run_id FOR UPDATE;
            SELECT * INTO command_row FROM public.run_command AS c
             WHERE c.tenant_id = p_tenant_id AND c.command_id = p_command_id FOR UPDATE;
            authority_now := clock_timestamp();
            IF command_row.command_id IS NOT NULL
               AND run_row.run_id IS NOT NULL
               AND command_row.run_id = p_run_id
               AND command_row.command_seq = p_command_seq
               AND command_row.command_digest = p_command_digest
               AND command_row.status = 'leased'
               AND command_row.superseded_by_command_id IS NULL
               AND (
                    NOT public.grove_execution_claim_lifecycle_valid(run_row.status, command_row.command_type)
                    OR command_row.lease_until IS NULL OR command_row.lease_until <= authority_now
                    OR run_row.lease_until IS NULL OR run_row.lease_until <= authority_now
               ) THEN
                RETURN QUERY SELECT 'stale'::TEXT, command_row.command_id, command_row.tenant_id,
                    command_row.run_id, command_row.command_seq, command_row.command_type,
                    command_row.command_schema_version, command_row.command_digest,
                    p_runtime_build_hash, command_row.status;
                RETURN;
            END IF;
            RETURN QUERY SELECT * FROM public.grove_consume_run_command_internal(
                p_tenant_id, p_run_id, p_command_id, p_command_seq, p_command_digest,
                p_runtime_build_hash, p_worker_id, p_execution_fence, p_expected_lease_until
            );
        END
        $$
    """


def _dead_letter_wrapper_sql() -> str:
    return """
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
            run_row public.agent_run%ROWTYPE;
            command_row public.run_command%ROWTYPE;
            authority_now TIMESTAMPTZ;
        BEGIN
            IF session_user <> 'grove_runtime' THEN
                RAISE EXCEPTION 'runtime dead-letter role required' USING ERRCODE = '42501';
            END IF;
            IF NULLIF(current_setting('grove.tenant_id', true), '') IS DISTINCT FROM p_tenant_id THEN
                RAISE EXCEPTION 'runtime tenant scope mismatch' USING ERRCODE = '42501';
            END IF;
            SELECT * INTO run_row FROM public.agent_run AS r
             WHERE r.tenant_id = p_tenant_id AND r.run_id = p_run_id FOR UPDATE;
            SELECT * INTO command_row FROM public.run_command AS c
             WHERE c.tenant_id = p_tenant_id AND c.command_id = p_command_id FOR UPDATE;
            authority_now := clock_timestamp();
            IF run_row.run_id IS NULL OR command_row.command_id IS NULL
               OR command_row.run_id <> p_run_id
               OR command_row.command_seq <> p_command_seq
               OR command_row.command_digest <> p_command_digest
               OR command_row.status <> 'leased'
               OR command_row.lease_owner <> p_worker_id
               OR command_row.execution_fence <> p_execution_fence
               OR command_row.lease_until IS DISTINCT FROM p_expected_lease_until
               OR run_row.lease_owner <> p_worker_id
               OR run_row.runtime_build_hash <> p_runtime_build_hash
               OR run_row.execution_fence <> p_execution_fence
               OR run_row.lease_until IS DISTINCT FROM p_expected_lease_until
               OR NOT public.grove_execution_claim_lifecycle_valid(run_row.status, command_row.command_type) THEN
                IF command_row.command_id IS NULL THEN
                    RETURN QUERY SELECT 'stale'::TEXT, p_command_id, p_tenant_id, p_run_id,
                        p_command_seq, NULL::TEXT, NULL::TEXT, p_command_digest,
                        p_runtime_build_hash, 'unknown'::TEXT;
                ELSE
                    RETURN QUERY SELECT 'stale'::TEXT, command_row.command_id, command_row.tenant_id,
                        command_row.run_id, command_row.command_seq, command_row.command_type,
                        command_row.command_schema_version, command_row.command_digest,
                        p_runtime_build_hash, command_row.status;
                END IF;
                RETURN;
            END IF;
            IF command_row.lease_until <= authority_now OR run_row.lease_until <= authority_now THEN
                RETURN QUERY SELECT 'expired'::TEXT, command_row.command_id, command_row.tenant_id,
                    command_row.run_id, command_row.command_seq, command_row.command_type,
                    command_row.command_schema_version, command_row.command_digest,
                    p_runtime_build_hash, command_row.status;
                RETURN;
            END IF;
            RETURN QUERY SELECT * FROM public.grove_dead_letter_run_command_internal(
                p_tenant_id, p_run_id, p_command_id, p_command_seq, p_command_digest,
                p_runtime_build_hash, p_worker_id, p_execution_fence,
                p_expected_lease_until, p_reason_ref
            );
        END
        $$
    """


def _reconcile_wrapper_sql() -> str:
    return """
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
            run_row public.agent_run%ROWTYPE;
            leased_command_row public.run_command%ROWTYPE;
            first_command public.run_command%ROWTYPE;
            leased_count INTEGER := 0;
            authority_now TIMESTAMPTZ;
        BEGIN
            IF session_user <> 'grove_projection' THEN
                RAISE EXCEPTION 'projection reconciliation role required' USING ERRCODE = '42501';
            END IF;
            IF NULLIF(current_setting('grove.tenant_id', true), '') IS DISTINCT FROM p_tenant_id
               OR p_run_id IS NULL THEN
                RAISE EXCEPTION 'invalid projection reconciliation arguments' USING ERRCODE = '22023';
            END IF;
            SELECT * INTO run_row FROM public.agent_run AS r
             WHERE r.tenant_id = p_tenant_id AND r.run_id = p_run_id FOR UPDATE;
            IF NOT FOUND THEN
                RETURN QUERY SELECT * FROM public.grove_reconcile_expired_run_command_internal(p_tenant_id, p_run_id);
                RETURN;
            END IF;
            FOR leased_command_row IN
                SELECT c.* FROM public.run_command AS c
                 WHERE c.tenant_id = p_tenant_id AND c.run_id = p_run_id AND c.status = 'leased'
                 ORDER BY c.command_seq, c.command_id FOR UPDATE
            LOOP
                leased_count := leased_count + 1;
                IF leased_count = 1 THEN
                    first_command := leased_command_row;
                END IF;
            END LOOP;
            authority_now := clock_timestamp();
            IF leased_count = 1
               AND NOT public.grove_execution_claim_lifecycle_valid(run_row.status, first_command.command_type) THEN
                RETURN QUERY SELECT 'manual'::TEXT, first_command.command_id, first_command.tenant_id,
                    first_command.run_id, first_command.command_seq, first_command.command_type,
                    first_command.command_schema_version, first_command.command_digest,
                    run_row.runtime_build_hash, first_command.status;
                RETURN;
            END IF;
            RETURN QUERY SELECT * FROM public.grove_reconcile_expired_run_command_internal(p_tenant_id, p_run_id);
        END
        $$
    """


def _checkpoint_authority_sql() -> str:
    return """
        CREATE OR REPLACE FUNCTION grove_checkpoint_authority_guard() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE
            scoped_tenant TEXT;
            claim_command_id UUID;
            claim_run_id UUID;
            claim_command_seq BIGINT;
            claim_command_digest TEXT;
            claim_build_hash TEXT;
            claim_worker_id TEXT;
            claim_execution_fence BIGINT;
            claim_lease_until TIMESTAMPTZ;
            run_status TEXT;
            run_build_hash TEXT;
            run_lease_owner TEXT;
            run_execution_fence BIGINT;
            run_lease_until TIMESTAMPTZ;
            command_run_id UUID;
            command_seq BIGINT;
            command_digest TEXT;
            command_type TEXT;
            command_status TEXT;
            command_lease_owner TEXT;
            command_execution_fence BIGINT;
            command_lease_until TIMESTAMPTZ;
            authority_now TIMESTAMPTZ;
        BEGIN
            IF session_user <> 'grove_runtime' THEN
                RAISE EXCEPTION 'runtime checkpoint role required' USING ERRCODE = '42501';
            END IF;
            scoped_tenant := NULLIF(current_setting('grove.tenant_id', true), '');
            IF scoped_tenant IS NULL OR NEW.tenant_id IS DISTINCT FROM scoped_tenant THEN
                RAISE EXCEPTION 'runtime checkpoint tenant scope mismatch' USING ERRCODE = '42501';
            END IF;
            BEGIN
                claim_command_id := NULLIF(current_setting('grove.checkpoint.command_id', true), '')::UUID;
                claim_run_id := NULLIF(current_setting('grove.checkpoint.run_id', true), '')::UUID;
                claim_command_seq := NULLIF(current_setting('grove.checkpoint.command_seq', true), '')::BIGINT;
                claim_command_digest := NULLIF(current_setting('grove.checkpoint.command_digest', true), '');
                claim_build_hash := NULLIF(current_setting('grove.checkpoint.runtime_build_hash', true), '');
                claim_worker_id := NULLIF(current_setting('grove.checkpoint.worker_id', true), '');
                claim_execution_fence := NULLIF(current_setting('grove.checkpoint.execution_fence', true), '')::BIGINT;
                claim_lease_until := NULLIF(current_setting('grove.checkpoint.lease_until', true), '')::TIMESTAMPTZ;
            EXCEPTION WHEN others THEN
                RAISE EXCEPTION 'checkpoint claim context is malformed' USING ERRCODE = '22023';
            END;
            SELECT r.status, r.runtime_build_hash, r.lease_owner, r.execution_fence, r.lease_until
              INTO run_status, run_build_hash, run_lease_owner, run_execution_fence, run_lease_until
              FROM public.agent_run AS r
             WHERE r.tenant_id = scoped_tenant AND r.run_id = claim_run_id
             FOR UPDATE;
            SELECT c.run_id, c.command_seq, c.command_digest, c.command_type, c.status,
                   c.lease_owner, c.execution_fence, c.lease_until
              INTO command_run_id, command_seq, command_digest, command_type, command_status,
                   command_lease_owner, command_execution_fence, command_lease_until
              FROM public.run_command AS c
             WHERE c.tenant_id = scoped_tenant AND c.command_id = claim_command_id
             FOR UPDATE;
            authority_now := clock_timestamp();
            IF run_status IS NULL OR command_run_id IS NULL
               OR command_run_id <> claim_run_id
               OR command_seq <> claim_command_seq
               OR command_digest <> claim_command_digest
               OR command_status <> 'leased'
               OR command_lease_owner <> claim_worker_id
               OR command_execution_fence <> claim_execution_fence
               OR command_lease_until IS DISTINCT FROM claim_lease_until
               OR run_lease_owner <> claim_worker_id
               OR run_build_hash <> claim_build_hash
               OR run_execution_fence <> claim_execution_fence
               OR run_lease_until IS DISTINCT FROM claim_lease_until
               OR run_lease_until <= authority_now
               OR command_lease_until <= authority_now
               OR NOT public.grove_execution_claim_lifecycle_valid(run_status, command_type) THEN
                RAISE EXCEPTION 'checkpoint claim lifecycle or lease is stale' USING ERRCODE = '40001';
            END IF;
            RETURN NEW;
        END
        $$
    """


def _rename_internal_functions() -> None:
    op.execute(f"ALTER FUNCTION grove_claim_run_command({_CLAIM_SIGNATURE}) RENAME TO grove_claim_run_command_internal")
    op.execute(
        f"ALTER FUNCTION grove_heartbeat_run_command({_HEARTBEAT_SIGNATURE}) "
        "RENAME TO grove_heartbeat_run_command_internal"
    )
    op.execute(
        f"ALTER FUNCTION grove_consume_run_command({_CONSUME_SIGNATURE}) RENAME TO grove_consume_run_command_internal"
    )
    op.execute(
        f"ALTER FUNCTION grove_dead_letter_run_command({_DEAD_LETTER_SIGNATURE}) "
        "RENAME TO grove_dead_letter_run_command_internal"
    )
    op.execute(
        f"ALTER FUNCTION grove_reconcile_expired_run_command({_RECONCILE_SIGNATURE}) "
        "RENAME TO grove_reconcile_expired_run_command_internal"
    )
    for name, signature in (
        ("grove_claim_run_command_internal", _CLAIM_SIGNATURE),
        ("grove_heartbeat_run_command_internal", _HEARTBEAT_SIGNATURE),
        ("grove_consume_run_command_internal", _CONSUME_SIGNATURE),
        ("grove_dead_letter_run_command_internal", _DEAD_LETTER_SIGNATURE),
        ("grove_reconcile_expired_run_command_internal", _RECONCILE_SIGNATURE),
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {name}({signature}) FROM PUBLIC, grove_runtime, grove_projection")
    op.execute("ALTER FUNCTION grove_checkpoint_authority_guard() RENAME TO grove_checkpoint_physical_guard")
    op.execute("REVOKE ALL ON FUNCTION grove_checkpoint_physical_guard() FROM PUBLIC, grove_runtime")


def _rebind_checkpoint_triggers() -> None:
    for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_authority_guard ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_physical_guard ON {table}")
        op.execute(
            f"CREATE TRIGGER {table}_authority_guard BEFORE INSERT OR UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION grove_checkpoint_authority_guard()"
        )
        # PostgreSQL executes triggers with the same timing/event in name order;
        # this fixed catalog name keeps the physical 0004 guard after the new
        # lifecycle authority guard.  The evidence contract fixes both names,
        # definitions and target functions, and integration tests cover the
        # lock/proof interaction rather than relying on an unrecorded order.
        op.execute(
            f"CREATE TRIGGER {table}_physical_guard BEFORE INSERT OR UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION grove_checkpoint_physical_guard()"
        )


def _claim_downgrade_sql() -> str:
    """Restore the exact 0006 claim body without reading live catalog text."""

    return """
        CREATE OR REPLACE FUNCTION grove_claim_run_command(
            p_tenant_id TEXT,
            p_worker_id TEXT,
            p_runtime_build_hash TEXT,
            p_lease_seconds DOUBLE PRECISION
        ) RETURNS TABLE (
            result_code TEXT,
            command_id UUID,
            run_id UUID,
            command_seq BIGINT,
            command_digest TEXT,
            runtime_build_hash TEXT,
            execution_fence BIGINT,
            lease_until TIMESTAMPTZ
        )
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE
            candidate_command_id UUID;
            candidate_run_id UUID;
            candidate_command_seq BIGINT;
            candidate_command_digest TEXT;
            candidate_runtime_build_hash TEXT;
            locked_execution_fence BIGINT;
            next_fence BIGINT;
            next_lease_until TIMESTAMPTZ;
            database_now TIMESTAMPTZ;
            command_row public.run_command%ROWTYPE;
        BEGIN
            IF session_user <> 'grove_runtime' THEN
                RAISE EXCEPTION 'runtime claim role required' USING ERRCODE = '42501';
            END IF;
            IF NULLIF(current_setting('grove.tenant_id', true), '') IS DISTINCT FROM p_tenant_id THEN
                RAISE EXCEPTION 'runtime tenant scope mismatch' USING ERRCODE = '42501';
            END IF;
            IF p_worker_id IS NULL OR length(p_worker_id) NOT BETWEEN 1 AND 256
               OR p_runtime_build_hash IS NULL OR p_runtime_build_hash !~ '^[0-9a-f]{64}$'
               OR p_lease_seconds IS NULL OR p_lease_seconds <= 0 OR p_lease_seconds > 90
               OR p_lease_seconds = 'Infinity'::float8 OR p_lease_seconds = '-Infinity'::float8
               OR p_lease_seconds = 'NaN'::float8 THEN
                RAISE EXCEPTION 'invalid runtime claim arguments' USING ERRCODE = '22023';
            END IF;

            database_now := clock_timestamp();
            SELECT c.command_id, c.run_id, c.command_seq, c.command_digest,
                   r.runtime_build_hash, r.execution_fence
              INTO candidate_command_id, candidate_run_id, candidate_command_seq,
                   candidate_command_digest, candidate_runtime_build_hash, locked_execution_fence
              FROM public.agent_run AS r
              JOIN LATERAL (
                    SELECT c.command_id, c.run_id, c.command_seq, c.command_digest
                      FROM public.run_command AS c
                     WHERE c.tenant_id = p_tenant_id
                       AND c.run_id = r.run_id
                       AND c.superseded_by_command_id IS NULL
                       AND c.available_at <= database_now
                       AND (c.status = 'pending' OR (c.status = 'leased' AND c.lease_until <= database_now))
                     ORDER BY (c.command_type = 'cancel') DESC, c.available_at, c.created_at, c.command_id
                     LIMIT 1
              ) AS c ON TRUE
             WHERE r.tenant_id = p_tenant_id
               AND r.runtime_build_hash = p_runtime_build_hash
               AND r.status NOT IN ('succeeded', 'failed', 'cancelled')
               AND (r.lease_until IS NULL OR r.lease_until <= database_now)
             ORDER BY (c.command_id IS NOT NULL), c.command_id
             FOR UPDATE OF r, c SKIP LOCKED
             LIMIT 1;

            IF FOUND THEN
                SELECT * INTO command_row
                  FROM public.run_command AS c
                 WHERE c.tenant_id = p_tenant_id AND c.command_id = candidate_command_id
                 FOR UPDATE;
                IF FOUND AND command_row.superseded_by_command_id IS NULL
                   AND command_row.available_at <= database_now
                   AND (command_row.status = 'pending'
                        OR (command_row.status = 'leased' AND command_row.lease_until <= database_now)) THEN
                    IF locked_execution_fence >= 9223372036854775807 THEN
                        RETURN QUERY SELECT 'fence_exhausted'::TEXT, candidate_command_id, candidate_run_id,
                            candidate_command_seq, candidate_command_digest, candidate_runtime_build_hash,
                            locked_execution_fence, NULL::TIMESTAMPTZ;
                        RETURN;
                    END IF;
                    next_fence := locked_execution_fence + 1;
                    next_lease_until := database_now + make_interval(secs => p_lease_seconds);
                    UPDATE public.agent_run
                       SET execution_fence = next_fence,
                           lease_owner = p_worker_id,
                           lease_until = next_lease_until,
                           status = CASE WHEN status = 'accepted' THEN 'running' ELSE status END,
                           updated_at = clock_timestamp()
                     WHERE tenant_id = p_tenant_id AND agent_run.run_id = candidate_run_id;
                    UPDATE public.run_command
                       SET status = 'leased', lease_owner = p_worker_id,
                           lease_until = next_lease_until, execution_fence = next_fence,
                           attempt_count = attempt_count + 1
                     WHERE tenant_id = p_tenant_id AND run_command.command_id = candidate_command_id;

                    RETURN QUERY SELECT 'claimed'::TEXT, candidate_command_id, candidate_run_id,
                        candidate_command_seq, candidate_command_digest, candidate_runtime_build_hash,
                        next_fence, next_lease_until;
                    RETURN;
                END IF;
            END IF;

            IF EXISTS (
                SELECT 1
                  FROM public.run_command AS c
                  JOIN public.agent_run AS r
                    ON r.tenant_id = c.tenant_id AND r.run_id = c.run_id
                 WHERE c.tenant_id = p_tenant_id
                   AND r.runtime_build_hash = p_runtime_build_hash
                   AND r.status NOT IN ('succeeded', 'failed', 'cancelled')
                   AND c.superseded_by_command_id IS NULL
                   AND c.status IN ('pending', 'leased')
            ) THEN
                RETURN;
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM public.run_command AS c
                  JOIN public.agent_run AS r
                    ON r.tenant_id = c.tenant_id AND r.run_id = c.run_id
                 WHERE c.tenant_id = p_tenant_id
                   AND r.runtime_build_hash <> p_runtime_build_hash
                   AND r.status NOT IN ('succeeded', 'failed', 'cancelled')
                   AND c.superseded_by_command_id IS NULL
                   AND c.status IN ('pending', 'leased')
            ) THEN
                RETURN QUERY SELECT 'version_unavailable'::TEXT, NULL::UUID, NULL::UUID,
                    NULL::BIGINT, NULL::TEXT, NULL::TEXT, NULL::BIGINT, NULL::TIMESTAMPTZ;
            END IF;
            RETURN;
        END $$
    """


def upgrade() -> None:
    op.execute(_lifecycle_predicate_sql())
    op.execute("REVOKE ALL ON FUNCTION grove_execution_claim_lifecycle_valid(TEXT, TEXT) FROM PUBLIC")
    _rename_internal_functions()
    op.execute(_claim_wrapper_sql())
    op.execute(_heartbeat_wrapper_sql())
    op.execute(_consume_wrapper_sql())
    op.execute(_dead_letter_wrapper_sql())
    op.execute(_reconcile_wrapper_sql())
    op.execute(_checkpoint_authority_sql())
    _rebind_checkpoint_triggers()
    # The claim wrapper owns the exact candidate mutation itself.  The 0006
    # scanner body is not retained as a downgrade-only catalog surface.
    op.execute(f"DROP FUNCTION grove_claim_run_command_internal({_CLAIM_SIGNATURE})")
    op.execute(f"REVOKE ALL ON FUNCTION grove_claim_run_command({_CLAIM_SIGNATURE}) FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION grove_heartbeat_run_command({_HEARTBEAT_SIGNATURE}) FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION grove_consume_run_command({_CONSUME_SIGNATURE}) FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION grove_dead_letter_run_command({_DEAD_LETTER_SIGNATURE}) FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION grove_reconcile_expired_run_command({_RECONCILE_SIGNATURE}) FROM PUBLIC")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grove_runtime') THEN
                GRANT EXECUTE ON FUNCTION grove_claim_run_command(TEXT, TEXT, TEXT, DOUBLE PRECISION) TO grove_runtime;
                GRANT EXECUTE ON FUNCTION grove_heartbeat_run_command(
                    TEXT, UUID, UUID, BIGINT, TEXT, TEXT, TEXT, BIGINT, TIMESTAMPTZ, DOUBLE PRECISION
                ) TO grove_runtime;
                GRANT EXECUTE ON FUNCTION grove_consume_run_command(
                    TEXT, UUID, UUID, BIGINT, TEXT, TEXT, TEXT, BIGINT, TIMESTAMPTZ
                ) TO grove_runtime;
                GRANT EXECUTE ON FUNCTION grove_dead_letter_run_command(
                    TEXT, UUID, UUID, BIGINT, TEXT, TEXT, TEXT, BIGINT, TIMESTAMPTZ, TEXT
                ) TO grove_runtime;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grove_projection') THEN
                GRANT EXECUTE ON FUNCTION grove_reconcile_expired_run_command(TEXT, UUID) TO grove_projection;
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_physical_guard ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_authority_guard ON {table}")
        op.execute(
            f"CREATE TRIGGER {table}_authority_guard BEFORE INSERT OR UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION grove_checkpoint_physical_guard()"
        )
    op.execute("DROP FUNCTION IF EXISTS grove_checkpoint_authority_guard()")
    op.execute("ALTER FUNCTION grove_checkpoint_physical_guard() RENAME TO grove_checkpoint_authority_guard")
    op.execute("DROP FUNCTION IF EXISTS grove_execution_claim_lifecycle_valid(TEXT, TEXT)")

    op.execute(f"DROP FUNCTION IF EXISTS grove_claim_run_command({_CLAIM_SIGNATURE})")
    op.execute(_claim_downgrade_sql())
    for name, signature, original in (
        ("grove_heartbeat_run_command", _HEARTBEAT_SIGNATURE, "grove_heartbeat_run_command_internal"),
        ("grove_consume_run_command", _CONSUME_SIGNATURE, "grove_consume_run_command_internal"),
        ("grove_dead_letter_run_command", _DEAD_LETTER_SIGNATURE, "grove_dead_letter_run_command_internal"),
        ("grove_reconcile_expired_run_command", _RECONCILE_SIGNATURE, "grove_reconcile_expired_run_command_internal"),
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {name}({signature})")
        op.execute(f"ALTER FUNCTION {original}({signature}) RENAME TO {name}")

    op.execute(f"REVOKE ALL ON FUNCTION grove_claim_run_command({_CLAIM_SIGNATURE}) FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION grove_heartbeat_run_command({_HEARTBEAT_SIGNATURE}) FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION grove_consume_run_command({_CONSUME_SIGNATURE}) FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION grove_dead_letter_run_command({_DEAD_LETTER_SIGNATURE}) FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION grove_reconcile_expired_run_command({_RECONCILE_SIGNATURE}) FROM PUBLIC")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grove_runtime') THEN
                GRANT EXECUTE ON FUNCTION grove_claim_run_command(TEXT, TEXT, TEXT, DOUBLE PRECISION) TO grove_runtime;
                GRANT EXECUTE ON FUNCTION grove_heartbeat_run_command(
                    TEXT, UUID, UUID, BIGINT, TEXT, TEXT, TEXT, BIGINT, TIMESTAMPTZ, DOUBLE PRECISION
                ) TO grove_runtime;
                GRANT EXECUTE ON FUNCTION grove_consume_run_command(
                    TEXT, UUID, UUID, BIGINT, TEXT, TEXT, TEXT, BIGINT, TIMESTAMPTZ
                ) TO grove_runtime;
                GRANT EXECUTE ON FUNCTION grove_dead_letter_run_command(
                    TEXT, UUID, UUID, BIGINT, TEXT, TEXT, TEXT, BIGINT, TIMESTAMPTZ, TEXT
                ) TO grove_runtime;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grove_projection') THEN
                GRANT EXECUTE ON FUNCTION grove_reconcile_expired_run_command(TEXT, UUID) TO grove_projection;
            END IF;
        END
        $$
        """
    )
