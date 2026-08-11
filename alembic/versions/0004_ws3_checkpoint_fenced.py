"""Add claim-bound LangGraph checkpoint persistence and consume fencing.

The pinned upstream schema is retained, but every checkpoint/blob/pending-write
row receives the same database-verified claim provenance and a digest of the
physical row material.  Runtime code only supplies a local claim context; the
triggers lock and re-check the durable command/run lease before accepting any
row.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "ws3_checkpoint_fenced"
down_revision: str | None = "ws3_execution_driver"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
_AUTHORITY_COLUMNS = (
    "claim_command_id",
    "claim_command_seq",
    "claim_command_digest",
    "claim_worker_id",
    "claim_execution_fence",
    "claim_lease_until",
    "claim_runtime_build_hash",
    "claim_provenance_hash",
)


def upgrade() -> None:
    op.execute("ALTER TABLE agent_run ADD COLUMN latest_checkpoint_id TEXT")
    op.execute("ALTER TABLE agent_run ADD COLUMN latest_applied_command_id UUID")
    op.execute("ALTER TABLE agent_run ADD COLUMN latest_applied_command_digest TEXT")
    op.execute("ALTER TABLE agent_run ADD COLUMN latest_applied_command_seq BIGINT")
    op.execute("ALTER TABLE run_command ADD COLUMN consumed_worker_id TEXT")
    op.execute("ALTER TABLE run_command ADD COLUMN consumed_execution_fence BIGINT")
    op.execute("ALTER TABLE run_command ADD COLUMN consumed_lease_until TIMESTAMPTZ")
    op.execute("ALTER TABLE run_command ADD COLUMN consumed_claim_provenance_hash TEXT")
    op.execute("ALTER TABLE run_command ADD COLUMN consumed_provenance_kind TEXT")
    op.execute("ALTER TABLE run_command ADD COLUMN superseded_by_command_id UUID")
    op.execute("ALTER TABLE run_command ADD COLUMN superseded_by_command_seq BIGINT")
    op.execute("ALTER TABLE run_command ADD COLUMN superseded_by_command_digest TEXT")
    op.execute("ALTER TABLE run_command ADD COLUMN superseded_by_provenance_hash TEXT")
    # 0003 allowed consumed commands but did not persist the claim identity
    # after clearing the active lease fields.  Those historical rows cannot be
    # upgraded into claim-bound proof without inventing worker/fence/lease
    # facts.  Mark them with a closed legacy discriminator before installing
    # the v1 proof constraint; runtime functions only write ``claim.v1``.
    op.execute("UPDATE run_command SET consumed_provenance_kind = 'legacy_unverified' WHERE status = 'consumed'")
    op.execute("ALTER TABLE command_payload DROP CONSTRAINT command_payload_schema_version_ck")
    op.execute(
        "ALTER TABLE command_payload ADD CONSTRAINT command_payload_schema_version_ck "
        "CHECK (command_schema_version IN ('start.v1', 'resume.v1', 'cancel.v1', 'continue.v1', 'signal.v1'))"
    )
    op.execute(
        "ALTER TABLE agent_run ADD CONSTRAINT agent_run_latest_applied_seq_ck "
        "CHECK (latest_applied_command_seq IS NULL OR latest_applied_command_seq >= 0)"
    )
    op.execute(
        "ALTER TABLE run_command ADD CONSTRAINT run_command_consumed_provenance_ck CHECK ("
        "(status = 'consumed' AND consumed_provenance_kind IS NOT NULL "
        "AND consumed_provenance_kind = 'claim.v1' "
        "AND consumed_worker_id IS NOT NULL "
        "AND consumed_execution_fence IS NOT NULL AND consumed_lease_until IS NOT NULL "
        "AND consumed_claim_provenance_hash ~ '^[0-9a-f]{64}$') OR "
        "(status = 'consumed' AND consumed_provenance_kind IS NOT NULL "
        "AND consumed_provenance_kind = 'legacy_unverified' "
        "AND consumed_worker_id IS NULL "
        "AND consumed_execution_fence IS NULL AND consumed_lease_until IS NULL "
        "AND consumed_claim_provenance_hash IS NULL) OR "
        "(status <> 'consumed' AND consumed_provenance_kind IS NULL "
        "AND consumed_worker_id IS NULL "
        "AND consumed_execution_fence IS NULL AND consumed_lease_until IS NULL "
        "AND consumed_claim_provenance_hash IS NULL))"
    )
    op.execute(
        "ALTER TABLE run_command ADD CONSTRAINT run_command_superseded_provenance_ck CHECK ("
        "(superseded_by_command_id IS NOT NULL AND superseded_by_command_seq IS NOT NULL "
        "AND superseded_by_command_digest IS NOT NULL "
        "AND superseded_by_command_digest ~ '^[0-9a-f]{64}$' "
        "AND (superseded_by_provenance_hash IS NULL OR "
        "superseded_by_provenance_hash ~ '^[0-9a-f]{64}$')) OR "
        "(superseded_by_command_id IS NULL AND superseded_by_command_seq IS NULL "
        "AND superseded_by_command_digest IS NULL AND superseded_by_provenance_hash IS NULL))"
    )
    op.execute(
        "ALTER TABLE run_command ADD CONSTRAINT run_command_superseded_target_fk "
        "FOREIGN KEY (tenant_id, superseded_by_command_id) "
        "REFERENCES run_command (tenant_id, command_id)"
    )

    op.execute("CREATE TABLE checkpoint_migrations (v INTEGER PRIMARY KEY)")
    op.execute("INSERT INTO checkpoint_migrations (v) SELECT value FROM generate_series(0, 9) AS value")
    op.execute(
        """
        CREATE TABLE checkpoints (
            tenant_id TEXT NOT NULL DEFAULT NULLIF(current_setting('grove.tenant_id', true), ''),
            thread_id TEXT NOT NULL,
            checkpoint_ns TEXT NOT NULL DEFAULT '',
            checkpoint_id TEXT NOT NULL,
            parent_checkpoint_id TEXT,
            type TEXT,
            checkpoint JSONB NOT NULL,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            content_hash TEXT,
            claim_command_id UUID,
            claim_command_seq BIGINT,
            claim_command_digest TEXT,
            claim_worker_id TEXT,
            claim_execution_fence BIGINT,
            claim_lease_until TIMESTAMPTZ,
            claim_runtime_build_hash TEXT,
            claim_provenance_hash TEXT,
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE checkpoint_blobs (
            tenant_id TEXT NOT NULL DEFAULT NULLIF(current_setting('grove.tenant_id', true), ''),
            thread_id TEXT NOT NULL,
            checkpoint_ns TEXT NOT NULL DEFAULT '',
            channel TEXT NOT NULL,
            version TEXT NOT NULL,
            type TEXT NOT NULL,
            blob BYTEA,
            content_hash TEXT,
            claim_command_id UUID,
            claim_command_seq BIGINT,
            claim_command_digest TEXT,
            claim_worker_id TEXT,
            claim_execution_fence BIGINT,
            claim_lease_until TIMESTAMPTZ,
            claim_runtime_build_hash TEXT,
            claim_provenance_hash TEXT,
            PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE checkpoint_writes (
            tenant_id TEXT NOT NULL DEFAULT NULLIF(current_setting('grove.tenant_id', true), ''),
            thread_id TEXT NOT NULL,
            checkpoint_ns TEXT NOT NULL DEFAULT '',
            checkpoint_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            idx INTEGER NOT NULL,
            channel TEXT NOT NULL,
            type TEXT,
            blob BYTEA NOT NULL,
            task_path TEXT NOT NULL DEFAULT '',
            content_hash TEXT,
            claim_command_id UUID,
            claim_command_seq BIGINT,
            claim_command_digest TEXT,
            claim_worker_id TEXT,
            claim_execution_fence BIGINT,
            claim_lease_until TIMESTAMPTZ,
            claim_runtime_build_hash TEXT,
            claim_provenance_hash TEXT,
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
        )
        """
    )
    op.execute("CREATE INDEX checkpoints_thread_id_idx ON checkpoints(thread_id)")
    op.execute("CREATE INDEX checkpoint_blobs_thread_id_idx ON checkpoint_blobs(thread_id)")
    op.execute("CREATE INDEX checkpoint_writes_thread_id_idx ON checkpoint_writes(thread_id)")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION grove_checkpoint_claim_provenance(
            p_tenant_id TEXT,
            p_run_id UUID,
            p_command_id UUID,
            p_command_seq BIGINT,
            p_command_digest TEXT,
            p_runtime_build_hash TEXT,
            p_worker_id TEXT,
            p_execution_fence BIGINT,
            p_lease_until TIMESTAMPTZ
        ) RETURNS TEXT
        LANGUAGE SQL IMMUTABLE STRICT SECURITY INVOKER
        SET search_path = pg_catalog, public AS $$
            SELECT encode(
                sha256(
                    convert_to(
                        concat_ws(
                            E'\\x1f', p_tenant_id, p_run_id::TEXT, p_command_id::TEXT,
                            p_command_seq::TEXT, p_command_digest, p_runtime_build_hash,
                            p_worker_id, p_execution_fence::TEXT,
                            round(extract(epoch FROM p_lease_until) * 1000000)::BIGINT::TEXT
                        ), 'UTF8'
                    )
                ), 'hex'
            )
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION grove_checkpoint_tenant_guard() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE
            scoped_tenant TEXT;
        BEGIN
            IF session_user <> 'grove_runtime' THEN
                RAISE EXCEPTION 'runtime checkpoint role required' USING ERRCODE = '42501';
            END IF;
            scoped_tenant := NULLIF(current_setting('grove.tenant_id', true), '');
            IF scoped_tenant IS NULL OR NEW.tenant_id IS DISTINCT FROM scoped_tenant THEN
                RAISE EXCEPTION 'runtime checkpoint tenant scope mismatch' USING ERRCODE = '42501';
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.tenant_id IS DISTINCT FROM NEW.tenant_id THEN
                RAISE EXCEPTION 'checkpoint tenant identity is immutable' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    for table in _CHECKPOINT_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_tenant_guard BEFORE INSERT OR UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION grove_checkpoint_tenant_guard()"
        )

    # One trigger function is deliberately shared by all three storage tables:
    # a runtime bypass must satisfy the same claim context regardless of which
    # physical row family it targets.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION grove_checkpoint_authority_guard() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE
            command_row RECORD;
            run_row RECORD;
            scoped_tenant TEXT;
            claim_command_id UUID;
            claim_run_id UUID;
            claim_command_seq BIGINT;
            claim_command_digest TEXT;
            claim_build_hash TEXT;
            claim_worker_id TEXT;
            claim_execution_fence BIGINT;
            claim_lease_until TIMESTAMPTZ;
            claim_provenance_hash TEXT;
            expected_checkpoint_ref TEXT;
            base_metadata JSONB;
            required_blob_channels JSONB;
            required_blob_channel TEXT;
            required_blob_version TEXT;
            existing_aux RECORD;
            blob_material TEXT;
            derived_hash TEXT;
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
            IF claim_command_id IS NULL OR claim_run_id IS NULL OR claim_command_seq IS NULL OR claim_command_seq < 0
               OR claim_command_digest IS NULL OR claim_command_digest !~ '^[0-9a-f]{64}$'
               OR claim_build_hash IS NULL OR claim_build_hash !~ '^[0-9a-f]{64}$'
               OR claim_worker_id IS NULL OR length(claim_worker_id) NOT BETWEEN 1 AND 256
               OR claim_execution_fence IS NULL OR claim_execution_fence < 1
               OR claim_lease_until IS NULL THEN
                RAISE EXCEPTION 'checkpoint claim context is incomplete' USING ERRCODE = '22023';
            END IF;
            claim_provenance_hash := grove_checkpoint_claim_provenance(
                scoped_tenant, claim_run_id, claim_command_id, claim_command_seq,
                claim_command_digest, claim_build_hash, claim_worker_id,
                claim_execution_fence, claim_lease_until
            );

            SELECT r.run_id, r.runtime_build_hash, r.lease_owner, r.execution_fence, r.lease_until,
                   r.latest_applied_command_seq, r.latest_applied_command_id, r.latest_applied_command_digest
              INTO run_row
              FROM public.agent_run AS r
             WHERE r.tenant_id = scoped_tenant AND r.run_id = claim_run_id
             FOR UPDATE;
            SELECT c.command_id, c.run_id, c.command_seq, c.command_digest,
                   c.command_type, c.status, c.lease_owner, c.execution_fence, c.lease_until
              INTO command_row
              FROM public.run_command AS c
             WHERE c.tenant_id = scoped_tenant AND c.command_id = claim_command_id
             FOR UPDATE;
            IF run_row.run_id IS NULL OR command_row.command_id IS NULL
               OR command_row.run_id <> claim_run_id
               OR command_row.command_seq <> claim_command_seq
               OR command_row.command_digest <> claim_command_digest
               OR command_row.status <> 'leased'
               OR command_row.lease_owner <> claim_worker_id
               OR command_row.execution_fence <> claim_execution_fence
               OR command_row.lease_until IS DISTINCT FROM claim_lease_until
               OR command_row.lease_until <= clock_timestamp()
               OR run_row.lease_owner <> claim_worker_id
               OR run_row.execution_fence <> claim_execution_fence
               OR run_row.lease_until IS DISTINCT FROM claim_lease_until
               OR run_row.lease_until <= clock_timestamp()
               OR run_row.runtime_build_hash <> claim_build_hash THEN
                RAISE EXCEPTION 'checkpoint claim is stale or forged' USING ERRCODE = '40001';
            END IF;
            IF NEW.thread_id IS DISTINCT FROM claim_run_id::TEXT THEN
                RAISE EXCEPTION 'checkpoint thread/run binding mismatch' USING ERRCODE = '42501';
            END IF;
            IF TG_OP = 'UPDATE' AND (
                OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
                OR OLD.thread_id IS DISTINCT FROM NEW.thread_id
                OR OLD.checkpoint_ns IS DISTINCT FROM NEW.checkpoint_ns
            ) THEN
                RAISE EXCEPTION 'checkpoint identity is immutable' USING ERRCODE = '23514';
            END IF;

            IF TG_TABLE_NAME = 'checkpoints' THEN
                IF TG_OP = 'UPDATE' AND OLD.checkpoint_id IS DISTINCT FROM NEW.checkpoint_id THEN
                    RAISE EXCEPTION 'checkpoint identity is immutable' USING ERRCODE = '23514';
                END IF;
                IF NEW.metadata IS NULL OR jsonb_typeof(NEW.metadata) <> 'object'
                   OR jsonb_typeof(NEW.checkpoint) <> 'object'
                   OR jsonb_typeof(NEW.checkpoint->'channel_versions') <> 'object' THEN
                    RAISE EXCEPTION 'checkpoint physical JSON must be an object' USING ERRCODE = '22023';
                END IF;
                expected_checkpoint_ref := 'checkpoint://' || NEW.thread_id || '/' ||
                    CASE WHEN NEW.checkpoint_ns = '' THEN '_root_' ELSE NEW.checkpoint_ns END ||
                    '/' || NEW.checkpoint_id;
                base_metadata := NEW.metadata - ARRAY[
                    'tenant_id', 'run_id', 'applied_command_id', 'applied_command_seq',
                    'applied_command_digest', 'runtime_build_hash', 'worker_id',
                    'execution_fence', 'lease_until', 'claim_fingerprint',
                    'claim_provenance_hash', 'checkpoint_ref', 'checkpoint_hash',
                    'checkpoint_content_hash'
                ];
                BEGIN
                    required_blob_channels := COALESCE(
                        NULLIF(current_setting('grove.checkpoint.blob_channels', true), '')::JSONB,
                        '[]'::JSONB
                    );
                EXCEPTION WHEN others THEN
                    RAISE EXCEPTION 'checkpoint blob closure context is malformed' USING ERRCODE = '22023';
                END;
                IF jsonb_typeof(required_blob_channels) <> 'array' THEN
                    RAISE EXCEPTION 'checkpoint blob closure context must be an array' USING ERRCODE = '22023';
                END IF;
                FOR required_blob_channel IN
                    SELECT jsonb_array_elements_text(required_blob_channels)
                LOOP
                    required_blob_version := NEW.checkpoint->'channel_versions'->>required_blob_channel;
                    SELECT required_blob.type, required_blob.blob
                      INTO existing_aux
                      FROM public.checkpoint_blobs AS required_blob
                     WHERE required_blob.tenant_id = scoped_tenant
                       AND required_blob.thread_id = NEW.thread_id
                       AND required_blob.checkpoint_ns = NEW.checkpoint_ns
                       AND required_blob.channel = required_blob_channel
                       AND required_blob.version = required_blob_version;
                    IF required_blob_version IS NULL OR NOT FOUND THEN
                        RAISE EXCEPTION 'checkpoint blob closure is missing' USING ERRCODE = '23514';
                    END IF;
                    IF existing_aux.type = 'empty' THEN
                        IF existing_aux.blob IS NULL THEN
                            RAISE EXCEPTION 'checkpoint primitive marker is missing' USING ERRCODE = '23514';
                        END IF;
                    ELSIF existing_aux.blob IS NULL THEN
                        RAISE EXCEPTION 'checkpoint blob payload is missing' USING ERRCODE = '23514';
                    END IF;
                END LOOP;
                SELECT coalesce(
                    string_agg(
                        format('%s|%s|%s|%s', b.channel, b.version, b.type, coalesce(encode(b.blob, 'hex'), '<null>')),
                        E'\\x1e' ORDER BY b.channel, b.version, b.type
                    ), ''
                )
                  INTO blob_material
                  FROM public.checkpoint_blobs AS b
                 WHERE b.tenant_id = scoped_tenant
                   AND b.thread_id = NEW.thread_id
                   AND b.checkpoint_ns = NEW.checkpoint_ns
                   AND EXISTS (
                       SELECT 1 FROM jsonb_each_text(NEW.checkpoint->'channel_versions') AS versions(channel, version)
                        WHERE versions.channel = b.channel AND versions.version = b.version
                   );
                derived_hash := encode(
                    sha256(
                        convert_to(
                            concat_ws(
                                E'\\x1f', scoped_tenant, NEW.thread_id, NEW.checkpoint_ns, NEW.checkpoint_id,
                                coalesce(NEW.parent_checkpoint_id, '<null>'), coalesce(NEW.type, '<null>'),
                                NEW.checkpoint::TEXT, base_metadata::TEXT, blob_material
                            ), 'UTF8'
                        )
                    ), 'hex'
                );
                IF (NEW.metadata ? 'checkpoint_ref' AND
                    NEW.metadata->>'checkpoint_ref' IS DISTINCT FROM expected_checkpoint_ref)
                   OR (NEW.metadata ? 'checkpoint_hash' AND
                       NEW.metadata->>'checkpoint_hash' IS DISTINCT FROM derived_hash) THEN
                    RAISE EXCEPTION 'checkpoint supplied proof does not match physical bytes' USING ERRCODE = '22023';
                END IF;
                IF run_row.latest_applied_command_seq IS NOT NULL
                   AND claim_command_seq < run_row.latest_applied_command_seq THEN
                    RAISE EXCEPTION 'checkpoint command sequence is below durable latest' USING ERRCODE = '40001';
                END IF;
                IF run_row.latest_applied_command_seq = claim_command_seq
                   AND (run_row.latest_applied_command_id IS DISTINCT FROM claim_command_id
                        OR run_row.latest_applied_command_digest IS DISTINCT FROM claim_command_digest) THEN
                    RAISE EXCEPTION 'checkpoint command sequence conflicts with durable latest' USING ERRCODE = '40001';
                END IF;
                IF EXISTS (
                    SELECT 1
                      FROM public.run_command AS older_command
                     WHERE older_command.tenant_id = scoped_tenant
                       AND older_command.run_id = claim_run_id
                       AND older_command.command_seq < claim_command_seq
                       AND older_command.status IN ('pending', 'leased')
                       AND older_command.superseded_by_command_id IS NOT NULL
                       AND (
                           command_row.command_type <> 'cancel'
                           OR older_command.superseded_by_command_id IS DISTINCT FROM claim_command_id
                           OR older_command.superseded_by_command_seq IS DISTINCT FROM claim_command_seq
                           OR older_command.superseded_by_command_digest IS DISTINCT FROM claim_command_digest
                           OR (older_command.superseded_by_provenance_hash IS NOT NULL
                               AND older_command.superseded_by_provenance_hash IS DISTINCT FROM claim_provenance_hash)
                       )
                ) THEN
                    RAISE EXCEPTION 'checkpoint supersede closure targets a different command' USING ERRCODE = '40001';
                END IF;
                IF EXISTS (
                    SELECT 1
                      FROM public.run_command AS older_command
                     WHERE older_command.tenant_id = scoped_tenant
                       AND older_command.run_id = claim_run_id
                       AND older_command.command_seq < claim_command_seq
                       AND older_command.status IN ('pending', 'leased')
                       AND older_command.superseded_by_command_id IS NULL
                       AND NOT EXISTS (
                           SELECT 1
                             FROM public.checkpoints AS older_checkpoint
                            WHERE older_checkpoint.tenant_id = scoped_tenant
                              AND older_checkpoint.thread_id = claim_run_id::TEXT
                              AND older_checkpoint.claim_command_id = older_command.command_id
                              AND older_checkpoint.claim_command_seq = older_command.command_seq
                              AND older_checkpoint.claim_command_digest = older_command.command_digest
                              AND older_checkpoint.claim_runtime_build_hash = claim_build_hash
                       )
                ) THEN
                    RAISE EXCEPTION 'checkpoint requires explicit supersede closure for older '
                        'command' USING ERRCODE = '40001';
                END IF;
                IF TG_OP = 'UPDATE'
                   AND OLD.content_hash = derived_hash
                   AND OLD.parent_checkpoint_id IS NOT DISTINCT FROM NEW.parent_checkpoint_id
                   AND OLD.type IS NOT DISTINCT FROM NEW.type
                   AND OLD.checkpoint IS NOT DISTINCT FROM NEW.checkpoint
                   AND (OLD.metadata - ARRAY[
                       'tenant_id', 'run_id', 'applied_command_id', 'applied_command_seq',
                       'applied_command_digest', 'runtime_build_hash', 'worker_id',
                       'execution_fence', 'lease_until', 'claim_fingerprint',
                       'claim_provenance_hash', 'checkpoint_ref', 'checkpoint_hash',
                       'checkpoint_content_hash'
                   ]) IS NOT DISTINCT FROM base_metadata
                THEN
                    -- Same-PK retries after a lease takeover are physical no-ops.
                    -- Preserve the apply-time proof and projection instead of
                    -- rebinding the row to the current claim.
                    NEW.content_hash := OLD.content_hash;
                    NEW.claim_command_id := OLD.claim_command_id;
                    NEW.claim_command_seq := OLD.claim_command_seq;
                    NEW.claim_command_digest := OLD.claim_command_digest;
                    NEW.claim_worker_id := OLD.claim_worker_id;
                    NEW.claim_execution_fence := OLD.claim_execution_fence;
                    NEW.claim_lease_until := OLD.claim_lease_until;
                    NEW.claim_runtime_build_hash := OLD.claim_runtime_build_hash;
                    NEW.claim_provenance_hash := OLD.claim_provenance_hash;
                    NEW.metadata := OLD.metadata;
                    RETURN NEW;
                END IF;
                IF TG_OP = 'UPDATE' THEN
                    RAISE EXCEPTION 'checkpoint identity/content conflict' USING ERRCODE = '23514';
                END IF;
                NEW.content_hash := derived_hash;
                NEW.claim_command_id := claim_command_id;
                NEW.claim_command_seq := claim_command_seq;
                NEW.claim_command_digest := claim_command_digest;
                NEW.claim_worker_id := claim_worker_id;
                NEW.claim_execution_fence := claim_execution_fence;
                NEW.claim_lease_until := claim_lease_until;
                NEW.claim_runtime_build_hash := claim_build_hash;
                NEW.claim_provenance_hash := claim_provenance_hash;
                NEW.metadata := base_metadata || jsonb_build_object(
                    'tenant_id', scoped_tenant, 'run_id', claim_run_id::TEXT,
                    'applied_command_id', claim_command_id::TEXT, 'applied_command_seq', claim_command_seq,
                    'applied_command_digest', claim_command_digest, 'runtime_build_hash', claim_build_hash,
                    'worker_id', claim_worker_id, 'execution_fence', claim_execution_fence,
                    'lease_until', claim_lease_until, 'claim_provenance_hash', claim_provenance_hash,
                    'checkpoint_ref', expected_checkpoint_ref, 'checkpoint_hash', derived_hash
                );
                UPDATE public.agent_run
                   SET latest_checkpoint_id = NEW.checkpoint_id,
                       latest_applied_command_id = claim_command_id,
                       latest_applied_command_digest = claim_command_digest,
                       latest_applied_command_seq = claim_command_seq,
                       updated_at = clock_timestamp()
                 WHERE tenant_id = scoped_tenant AND run_id = claim_run_id
                   AND (latest_applied_command_seq IS NULL OR latest_applied_command_seq <= claim_command_seq);
                -- Acceptance authority must create the relationship first;
                -- this trigger only binds the applied checkpoint provenance.
                UPDATE public.run_command AS superseded_command
                   SET superseded_by_provenance_hash = claim_provenance_hash
                 WHERE superseded_command.tenant_id = scoped_tenant
                   AND superseded_command.run_id = claim_run_id
                   AND superseded_command.command_seq < claim_command_seq
                   AND superseded_command.status IN ('pending', 'leased')
                   AND superseded_command.superseded_by_command_id = claim_command_id
                   AND superseded_command.superseded_by_command_seq = claim_command_seq
                   AND superseded_command.superseded_by_command_digest = claim_command_digest
                   AND superseded_command.superseded_by_provenance_hash IS NULL;
            ELSE
                IF TG_TABLE_NAME = 'checkpoint_blobs' THEN
                    -- The pinned saver historically used DO NOTHING for blob
                    -- conflicts.  Check the existing physical row while the
                    -- command/run locks are held so a different blob at the
                    -- same version is a deterministic conflict, not a silent
                    -- reuse.
                    IF TG_OP = 'INSERT' THEN
                        SELECT b.channel, b.version, b.type, b.blob
                          INTO existing_aux
                          FROM public.checkpoint_blobs AS b
                         WHERE b.tenant_id = scoped_tenant
                           AND b.thread_id = NEW.thread_id
                           AND b.checkpoint_ns = NEW.checkpoint_ns
                           AND b.channel = NEW.channel
                           AND b.version = NEW.version
                         FOR UPDATE;
                        IF FOUND AND (
                            existing_aux.type IS DISTINCT FROM NEW.type
                            OR existing_aux.blob IS DISTINCT FROM NEW.blob
                        ) THEN
                            RAISE EXCEPTION 'checkpoint blob identity/content conflict' USING ERRCODE = '23514';
                        END IF;
                    END IF;
                    derived_hash := encode(
                        sha256(convert_to(concat_ws(E'\\x1f', scoped_tenant, NEW.thread_id, NEW.checkpoint_ns,
                            NEW.channel, NEW.version, NEW.type, coalesce(encode(NEW.blob, 'hex'), '<null>')), 'UTF8')),
                        'hex'
                    );
                    IF TG_OP = 'UPDATE'
                       AND OLD.content_hash = derived_hash
                       AND OLD.channel IS NOT DISTINCT FROM NEW.channel
                       AND OLD.type IS NOT DISTINCT FROM NEW.type
                       AND OLD.blob IS NOT DISTINCT FROM NEW.blob
                    THEN
                        NEW.content_hash := OLD.content_hash;
                        NEW.claim_command_id := OLD.claim_command_id;
                        NEW.claim_command_seq := OLD.claim_command_seq;
                        NEW.claim_command_digest := OLD.claim_command_digest;
                        NEW.claim_worker_id := OLD.claim_worker_id;
                        NEW.claim_execution_fence := OLD.claim_execution_fence;
                        NEW.claim_lease_until := OLD.claim_lease_until;
                        NEW.claim_runtime_build_hash := OLD.claim_runtime_build_hash;
                        NEW.claim_provenance_hash := OLD.claim_provenance_hash;
                        RETURN NEW;
                    END IF;
                ELSE
                    derived_hash := encode(
                        sha256(convert_to(concat_ws(E'\\x1f', scoped_tenant, NEW.thread_id, NEW.checkpoint_ns,
                            NEW.checkpoint_id, NEW.task_id, NEW.task_path, NEW.idx::TEXT, NEW.channel,
                            coalesce(NEW.type, '<null>'), encode(NEW.blob, 'hex')), 'UTF8')),
                        'hex'
                    );
                    IF TG_OP = 'UPDATE'
                       AND OLD.content_hash = derived_hash
                       AND OLD.channel IS NOT DISTINCT FROM NEW.channel
                       AND OLD.type IS NOT DISTINCT FROM NEW.type
                       AND OLD.blob IS NOT DISTINCT FROM NEW.blob
                       AND OLD.checkpoint_id IS NOT DISTINCT FROM NEW.checkpoint_id
                       AND OLD.task_id IS NOT DISTINCT FROM NEW.task_id
                       AND OLD.task_path IS NOT DISTINCT FROM NEW.task_path
                       AND OLD.idx IS NOT DISTINCT FROM NEW.idx
                    THEN
                        NEW.content_hash := OLD.content_hash;
                        NEW.claim_command_id := OLD.claim_command_id;
                        NEW.claim_command_seq := OLD.claim_command_seq;
                        NEW.claim_command_digest := OLD.claim_command_digest;
                        NEW.claim_worker_id := OLD.claim_worker_id;
                        NEW.claim_execution_fence := OLD.claim_execution_fence;
                        NEW.claim_lease_until := OLD.claim_lease_until;
                        NEW.claim_runtime_build_hash := OLD.claim_runtime_build_hash;
                        NEW.claim_provenance_hash := OLD.claim_provenance_hash;
                        RETURN NEW;
                    END IF;
                END IF;
                IF TG_OP = 'UPDATE' AND TG_TABLE_NAME = 'checkpoint_blobs'
                   AND OLD.content_hash = derived_hash
                   AND OLD.channel IS NOT DISTINCT FROM NEW.channel
                   AND OLD.type IS NOT DISTINCT FROM NEW.type
                   AND OLD.blob IS NOT DISTINCT FROM NEW.blob
                THEN
                    NEW.content_hash := OLD.content_hash;
                    NEW.claim_command_id := OLD.claim_command_id;
                    NEW.claim_command_seq := OLD.claim_command_seq;
                    NEW.claim_command_digest := OLD.claim_command_digest;
                    NEW.claim_worker_id := OLD.claim_worker_id;
                    NEW.claim_execution_fence := OLD.claim_execution_fence;
                    NEW.claim_lease_until := OLD.claim_lease_until;
                    NEW.claim_runtime_build_hash := OLD.claim_runtime_build_hash;
                    NEW.claim_provenance_hash := OLD.claim_provenance_hash;
                    RETURN NEW;
                END IF;
                IF TG_OP = 'UPDATE' THEN
                    RAISE EXCEPTION 'checkpoint auxiliary row content conflict' USING ERRCODE = '23514';
                END IF;
                NEW.content_hash := derived_hash;
                NEW.claim_command_id := claim_command_id;
                NEW.claim_command_seq := claim_command_seq;
                NEW.claim_command_digest := claim_command_digest;
                NEW.claim_worker_id := claim_worker_id;
                NEW.claim_execution_fence := claim_execution_fence;
                NEW.claim_lease_until := claim_lease_until;
                NEW.claim_runtime_build_hash := claim_build_hash;
                NEW.claim_provenance_hash := claim_provenance_hash;
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    for table in _CHECKPOINT_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_authority_guard BEFORE INSERT OR UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION grove_checkpoint_authority_guard()"
        )
        op.execute(f"ALTER TABLE {table} ALTER COLUMN content_hash SET NOT NULL")
        for column in _AUTHORITY_COLUMNS:
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET NOT NULL")
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {table}_content_hash_ck CHECK (content_hash ~ '^[0-9a-f]{{64}}$')"
        )
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {table}_claim_provenance_ck "
            "CHECK (claim_provenance_hash ~ '^[0-9a-f]{64}$')"
        )

    for table in _CHECKPOINT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant_policy ON {table} USING "
            "(tenant_id = NULLIF(current_setting('grove.tenant_id', true), '')) "
            "WITH CHECK (tenant_id = NULLIF(current_setting('grove.tenant_id', true), ''))"
        )

    op.execute(
        "REVOKE ALL ON checkpoint_migrations FROM PUBLIC, grove_api, grove_runtime, grove_projection, grove_governance"
    )
    op.execute(
        "REVOKE ALL ON checkpoints, checkpoint_blobs, checkpoint_writes "
        "FROM PUBLIC, grove_api, grove_projection, grove_governance"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON checkpoints, checkpoint_blobs, checkpoint_writes TO grove_runtime")
    op.execute("REVOKE DELETE ON checkpoints, checkpoint_blobs, checkpoint_writes FROM grove_runtime")

    op.execute(
        """
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
            command_row public.run_command%ROWTYPE;
            run_row public.agent_run%ROWTYPE;
            scoped_tenant TEXT;
            claim_provenance_hash TEXT;
            proof_exists BOOLEAN;
            superseded_proof_exists BOOLEAN;
        BEGIN
            IF session_user <> 'grove_runtime' THEN
                RAISE EXCEPTION 'runtime consume role required' USING ERRCODE = '42501';
            END IF;
            scoped_tenant := NULLIF(current_setting('grove.tenant_id', true), '');
            IF scoped_tenant IS NULL OR scoped_tenant IS DISTINCT FROM p_tenant_id
               OR p_run_id IS NULL OR p_command_id IS NULL OR p_worker_id IS NULL
               OR length(p_worker_id) NOT BETWEEN 1 AND 256
               OR p_command_seq IS NULL OR p_command_seq < 0
               OR p_command_digest IS NULL OR p_command_digest !~ '^[0-9a-f]{64}$'
               OR p_runtime_build_hash IS NULL OR p_runtime_build_hash !~ '^[0-9a-f]{64}$'
               OR p_execution_fence IS NULL OR p_execution_fence < 1
               OR p_expected_lease_until IS NULL THEN
                RAISE EXCEPTION 'invalid runtime consume arguments' USING ERRCODE = '22023';
            END IF;
            claim_provenance_hash := grove_checkpoint_claim_provenance(
                p_tenant_id, p_run_id, p_command_id, p_command_seq, p_command_digest,
                p_runtime_build_hash, p_worker_id, p_execution_fence, p_expected_lease_until
            );
            SELECT * INTO run_row
              FROM public.agent_run AS r
             WHERE r.tenant_id = p_tenant_id AND r.run_id = p_run_id
             FOR UPDATE;
            SELECT * INTO command_row
              FROM public.run_command AS c
             WHERE c.tenant_id = p_tenant_id AND c.command_id = p_command_id
             FOR UPDATE;
            IF run_row.run_id IS NULL OR command_row.command_id IS NULL
               OR command_row.run_id <> p_run_id
               OR command_row.command_seq <> p_command_seq
               OR command_row.command_digest <> p_command_digest
               OR run_row.runtime_build_hash <> p_runtime_build_hash THEN
                RETURN QUERY SELECT 'stale'::TEXT, p_command_id, p_tenant_id, p_run_id,
                    p_command_seq, NULL::TEXT, NULL::TEXT, p_command_digest, p_runtime_build_hash, 'unknown'::TEXT;
                RETURN;
            END IF;
            IF command_row.status = 'consumed' THEN
                IF command_row.consumed_worker_id = p_worker_id
                   AND command_row.consumed_execution_fence = p_execution_fence
                   AND command_row.consumed_lease_until = p_expected_lease_until
                   AND command_row.consumed_claim_provenance_hash = claim_provenance_hash THEN
                    RETURN QUERY SELECT 'consumed'::TEXT, command_row.command_id, command_row.tenant_id,
                        command_row.run_id, command_row.command_seq, command_row.command_type,
                        command_row.command_schema_version, command_row.command_digest,
                        p_runtime_build_hash, command_row.status;
                ELSE
                    RETURN QUERY SELECT 'stale'::TEXT, command_row.command_id, command_row.tenant_id,
                        command_row.run_id, command_row.command_seq, command_row.command_type,
                        command_row.command_schema_version, command_row.command_digest,
                        p_runtime_build_hash, command_row.status;
                END IF;
                RETURN;
            END IF;
            IF command_row.superseded_by_command_id IS NOT NULL THEN
                SELECT EXISTS (
                    SELECT 1
                      FROM public.run_command AS superseding_command
                      JOIN public.checkpoints AS superseding_checkpoint
                        ON superseding_checkpoint.tenant_id = superseding_command.tenant_id
                       AND superseding_checkpoint.thread_id = p_run_id::TEXT
                       AND superseding_checkpoint.checkpoint_id = run_row.latest_checkpoint_id
                     WHERE superseding_command.tenant_id = p_tenant_id
                       AND superseding_command.command_id = command_row.superseded_by_command_id
                       AND superseding_command.run_id = p_run_id
                       AND superseding_command.command_seq = command_row.superseded_by_command_seq
                       AND superseding_command.command_seq > p_command_seq
                       AND superseding_command.command_type = 'cancel'
                       AND superseding_command.command_digest = command_row.superseded_by_command_digest
                       AND superseding_command.status IN ('leased', 'consumed')
                       AND superseding_checkpoint.claim_command_id = superseding_command.command_id
                       AND superseding_checkpoint.claim_command_seq = superseding_command.command_seq
                       AND superseding_checkpoint.claim_command_digest = superseding_command.command_digest
                       AND superseding_checkpoint.claim_runtime_build_hash = p_runtime_build_hash
                       AND superseding_checkpoint.claim_provenance_hash = command_row.superseded_by_provenance_hash
                ) INTO superseded_proof_exists;
                IF superseded_proof_exists
                   AND command_row.status = 'leased'
                   AND command_row.lease_owner = p_worker_id
                   AND command_row.execution_fence = p_execution_fence
                   AND command_row.lease_until IS NOT DISTINCT FROM p_expected_lease_until THEN
                    UPDATE public.run_command AS consumed_command
                       SET status = 'consumed',
                           consumed_provenance_kind = 'claim.v1',
                           lease_owner = NULL,
                           lease_until = NULL,
                           execution_fence = NULL,
                           consumed_worker_id = p_worker_id,
                           consumed_execution_fence = p_execution_fence,
                           consumed_lease_until = p_expected_lease_until,
                           consumed_claim_provenance_hash = claim_provenance_hash
                     WHERE consumed_command.tenant_id = p_tenant_id
                       AND consumed_command.command_id = p_command_id;
                    UPDATE public.agent_run AS consumed_run
                       SET lease_owner = NULL,
                           lease_until = NULL,
                           updated_at = clock_timestamp()
                     WHERE consumed_run.tenant_id = p_tenant_id
                       AND consumed_run.run_id = p_run_id
                       AND consumed_run.lease_owner = p_worker_id
                       AND consumed_run.execution_fence = p_execution_fence
                       AND consumed_run.lease_until IS NOT DISTINCT FROM p_expected_lease_until;
                    RETURN QUERY SELECT 'consumed'::TEXT, command_row.command_id, command_row.tenant_id,
                        command_row.run_id, command_row.command_seq, command_row.command_type,
                        command_row.command_schema_version, command_row.command_digest,
                        p_runtime_build_hash, 'consumed'::TEXT;
                    RETURN;
                END IF;
            END IF;
            IF command_row.status <> 'leased'
               OR run_row.lease_owner <> p_worker_id
               OR command_row.lease_owner <> p_worker_id
               OR run_row.execution_fence <> p_execution_fence
               OR command_row.execution_fence <> p_execution_fence
               OR run_row.lease_until IS DISTINCT FROM p_expected_lease_until
               OR command_row.lease_until IS DISTINCT FROM p_expected_lease_until
               OR run_row.lease_until <= clock_timestamp()
               OR command_row.lease_until <= clock_timestamp() THEN
                RETURN QUERY SELECT 'stale'::TEXT, command_row.command_id, command_row.tenant_id,
                    command_row.run_id, command_row.command_seq, command_row.command_type,
                    command_row.command_schema_version, command_row.command_digest,
                    p_runtime_build_hash, command_row.status;
                RETURN;
            END IF;
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
                    p_runtime_build_hash, command_row.status;
                RETURN;
            END IF;
            UPDATE public.run_command AS consumed_command
               SET status = 'consumed',
                   consumed_provenance_kind = 'claim.v1',
                   lease_owner = NULL,
                   lease_until = NULL,
                   execution_fence = NULL,
                   consumed_worker_id = p_worker_id,
                   consumed_execution_fence = p_execution_fence,
                   consumed_lease_until = p_expected_lease_until,
                   consumed_claim_provenance_hash = claim_provenance_hash
             WHERE consumed_command.tenant_id = p_tenant_id AND consumed_command.command_id = p_command_id;
            UPDATE public.agent_run AS consumed_run
               SET lease_owner = NULL,
                   lease_until = NULL,
                   updated_at = clock_timestamp()
             WHERE consumed_run.tenant_id = p_tenant_id AND consumed_run.run_id = p_run_id;
            RETURN QUERY SELECT 'consumed'::TEXT, command_row.command_id, command_row.tenant_id,
                command_row.run_id, command_row.command_seq, command_row.command_type,
                command_row.command_schema_version, command_row.command_digest,
                p_runtime_build_hash, 'consumed'::TEXT;
        END
        $$
        """
    )
    op.execute(
        """
        REVOKE ALL ON FUNCTION grove_consume_run_command(
            TEXT, UUID, UUID, BIGINT, TEXT, TEXT, TEXT, BIGINT, TIMESTAMPTZ
        ) FROM PUBLIC
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grove_runtime') THEN
                GRANT EXECUTE ON FUNCTION grove_consume_run_command(
                    TEXT, UUID, UUID, BIGINT, TEXT, TEXT, TEXT, BIGINT, TIMESTAMPTZ
                ) TO grove_runtime;
            END IF;
        END
        $$
        """
    )
    # Runtime checkpoint writes must not create temporary relations that could
    # shadow the pinned public checkpoint tables.  Resolve the database name at
    # execution time so this migration remains portable across cleanrooms.
    op.execute(
        """
        DO $$
        BEGIN
            EXECUTE format(
                'REVOKE TEMP ON DATABASE %I FROM PUBLIC, grove_api, grove_runtime, '
                'grove_projection, grove_governance',
                current_database()
            );
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            EXECUTE format('GRANT TEMP ON DATABASE %I TO PUBLIC', current_database());
        END
        $$
        """
    )
    op.execute(
        """
        REVOKE ALL ON FUNCTION grove_consume_run_command(
            TEXT, UUID, UUID, BIGINT, TEXT, TEXT, TEXT, BIGINT, TIMESTAMPTZ
        ) FROM PUBLIC
        """
    )
    op.execute(
        """
        DROP FUNCTION IF EXISTS grove_consume_run_command(
            TEXT, UUID, UUID, BIGINT, TEXT, TEXT, TEXT, BIGINT, TIMESTAMPTZ
        )
        """
    )
    for table in _CHECKPOINT_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_authority_guard ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_tenant_guard ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_policy ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP FUNCTION IF EXISTS grove_checkpoint_authority_guard()")
    op.execute("DROP FUNCTION IF EXISTS grove_checkpoint_tenant_guard()")
    op.execute(
        "DROP FUNCTION IF EXISTS grove_checkpoint_claim_provenance("
        "TEXT, UUID, UUID, BIGINT, TEXT, TEXT, TEXT, BIGINT, TIMESTAMPTZ)"
    )
    for table in reversed(_CHECKPOINT_TABLES):
        op.execute(f"DROP TABLE {table}")
    op.execute("DROP TABLE checkpoint_migrations")
    op.execute("ALTER TABLE run_command DROP CONSTRAINT run_command_consumed_provenance_ck")
    # Downgrade intentionally discards both the discriminator and v1 proof.
    # A later re-upgrade classifies every remaining consumed row as legacy,
    # rather than pretending the discarded proof can be recovered.
    op.execute("ALTER TABLE run_command DROP CONSTRAINT run_command_superseded_provenance_ck")
    op.execute("ALTER TABLE run_command DROP CONSTRAINT run_command_superseded_target_fk")
    op.execute("ALTER TABLE agent_run DROP CONSTRAINT agent_run_latest_applied_seq_ck")
    for column in (
        "superseded_by_provenance_hash",
        "superseded_by_command_digest",
        "superseded_by_command_seq",
        "superseded_by_command_id",
        "consumed_provenance_kind",
        "consumed_claim_provenance_hash",
        "consumed_lease_until",
        "consumed_execution_fence",
        "consumed_worker_id",
    ):
        op.execute(f"ALTER TABLE run_command DROP COLUMN {column}")
    op.execute("ALTER TABLE command_payload DROP CONSTRAINT command_payload_schema_version_ck")
    op.execute(
        "ALTER TABLE command_payload ADD CONSTRAINT command_payload_schema_version_ck "
        "CHECK (command_schema_version = 'start.v1')"
    )
    op.execute("ALTER TABLE agent_run DROP COLUMN latest_applied_command_seq")
    op.execute("ALTER TABLE agent_run DROP COLUMN latest_applied_command_digest")
    op.execute("ALTER TABLE agent_run DROP COLUMN latest_applied_command_id")
    op.execute("ALTER TABLE agent_run DROP COLUMN latest_checkpoint_id")
