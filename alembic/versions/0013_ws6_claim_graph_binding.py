"""Carry the run's spec graph binding out of the claim authority function.

The runtime worker routes each claim to its execution kernel by the exact
graph binding owned by the run's SkillExecutionSpec.  The runtime role holds
no column-level read grant on execution_spec.spec_payload, so the routing
triple (graph ref/version/state-schema) surfaces through the same SECURITY
DEFINER claim function that already carries runtime_build_hash.  A claimed
run without a spec binding returns the empty triple; the worker's closed
graph registry dead-letters such commands.  Everything else in the function
is the 0007 authority wrapper verbatim (post-lock revalidation, CAS
subtransaction, GV001 semantics).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "ws6_claim_graph_binding"
down_revision: str | None = "ws3_consumed_provenance_compat"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CLAIM_SIGNATURE = "TEXT, TEXT, TEXT, DOUBLE PRECISION"

_GRANT_SQL = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grove_runtime') THEN
        GRANT EXECUTE ON FUNCTION grove_claim_run_command(TEXT, TEXT, TEXT, DOUBLE PRECISION) TO grove_runtime;
    END IF;
END
$$
"""


def _claim_graph_sql() -> str:
    return r"""
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
            lease_until TIMESTAMPTZ,
            graph_ref TEXT,
            graph_version TEXT,
            graph_state_schema_version TEXT
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
            v_graph_ref TEXT;
            v_graph_version TEXT;
            v_graph_state_schema TEXT;
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
                        run_row.execution_fence, NULL::TIMESTAMPTZ,
                        NULL::TEXT, NULL::TEXT, NULL::TEXT;
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
                -- Routing metadata is read after both locks from the run's
                -- authoritative spec.  A missing binding is a per-command
                -- dead-letter condition (empty triple), never a lease crash.
                SELECT COALESCE(es.spec_payload -> 'graph' -> 'graph' ->> 'ref', ''),
                       COALESCE(es.spec_payload -> 'graph' -> 'graph' ->> 'version', ''),
                       COALESCE(es.spec_payload -> 'graph' ->> 'graph_state_schema_version', '')
                  INTO v_graph_ref, v_graph_version, v_graph_state_schema
                  FROM public.execution_spec AS es
                 WHERE es.tenant_id = run_row.tenant_id
                   AND es.skill_spec_hash = run_row.skill_spec_hash
                   AND es.spec_ref = run_row.skill_spec_ref;
                RETURN QUERY SELECT 'claimed'::TEXT, command_row.command_id, run_row.run_id,
                    command_row.command_seq, command_row.command_digest, run_row.runtime_build_hash,
                    next_fence, next_lease_until,
                    COALESCE(v_graph_ref, ''), COALESCE(v_graph_version, ''),
                    COALESCE(v_graph_state_schema, '');
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
                    NULL::BIGINT, NULL::TEXT, NULL::TEXT, NULL::BIGINT, NULL::TIMESTAMPTZ,
                    NULL::TEXT, NULL::TEXT, NULL::TEXT;
            END IF;
            RETURN;
        END
        $$
"""


def _claim_legacy_sql() -> str:
    return r"""
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


def upgrade() -> None:
    # The return type changes, so the function must be replaced, not redefined.
    op.execute(f"DROP FUNCTION grove_claim_run_command({_CLAIM_SIGNATURE})")
    op.execute(_claim_graph_sql())
    op.execute(f"REVOKE ALL ON FUNCTION grove_claim_run_command({_CLAIM_SIGNATURE}) FROM PUBLIC")
    op.execute(_GRANT_SQL)


def downgrade() -> None:
    op.execute(f"DROP FUNCTION grove_claim_run_command({_CLAIM_SIGNATURE})")
    op.execute(_claim_legacy_sql())
    op.execute(f"REVOKE ALL ON FUNCTION grove_claim_run_command({_CLAIM_SIGNATURE}) FROM PUBLIC")
    op.execute(_GRANT_SQL)
