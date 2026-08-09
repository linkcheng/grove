"""WS-4 projection reconciliation helpers: cross-tenant outbox fetch and health.

The projection role owns a rebuildable read model but cannot enumerate tenants
through RLS without an active tenant context.  These SECURITY DEFINER helpers
give the reconciler a bounded, read-only view of pending outbox work across
tenants and a low-cardinality health aggregate.  They only read; all read-model
writes remain RLS-scoped to the per-row tenant context set by the projection.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "ws4_recon_helpers"
down_revision: str | None = "ws4_observation_slice"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BATCH_SIGNATURE = "INTEGER"
_HEALTH_SIGNATURE = ""


def _fetch_sql() -> str:
    return """
        CREATE OR REPLACE FUNCTION grove_fetch_observation_batch(p_limit INTEGER)
        RETURNS TABLE (
            outbox_id BIGINT,
            tenant_id TEXT,
            run_id UUID,
            event_id UUID,
            run_seq BIGINT,
            source TEXT
        )
        LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
            SELECT outbox_id, tenant_id, run_id, event_id, run_seq, source
              FROM public.runtime_event_outbox
             WHERE relayed_at IS NULL
             ORDER BY outbox_id
             LIMIT GREATEST(p_limit, 1)
        $$
    """


def _health_sql() -> str:
    return """
        CREATE OR REPLACE FUNCTION grove_observation_health()
        RETURNS TABLE (pending BIGINT, dead_letter BIGINT)
        LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
            SELECT
                COALESCE((SELECT count(*) FROM public.runtime_event_outbox WHERE relayed_at IS NULL), 0),
                COALESCE((SELECT count(*) FROM public.runtime_event_dead_letter), 0)
        $$
    """


def upgrade() -> None:
    op.execute(_fetch_sql())
    op.execute(_health_sql())
    op.execute(f"REVOKE ALL ON FUNCTION grove_fetch_observation_batch({_BATCH_SIGNATURE}) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION grove_fetch_observation_batch({_BATCH_SIGNATURE}) TO grove_projection")
    op.execute("REVOKE ALL ON FUNCTION grove_observation_health() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION grove_observation_health() TO grove_projection")

    # Rebuild is an explicit projection operation: it clears the rebuildable
    # read model before re-projecting from authoritative facts.
    op.execute("GRANT DELETE ON ui_projection_event, projection_watermark TO grove_projection")


def downgrade() -> None:
    op.execute("REVOKE ALL ON FUNCTION grove_observation_health() FROM PUBLIC, grove_projection")
    op.execute("DROP FUNCTION IF EXISTS grove_observation_health()")
    op.execute(f"REVOKE ALL ON FUNCTION grove_fetch_observation_batch({_BATCH_SIGNATURE}) FROM PUBLIC, grove_projection")
    op.execute(f"DROP FUNCTION IF EXISTS grove_fetch_observation_batch({_BATCH_SIGNATURE})")
