"""Asset Risk Reference Profile live-state table (docs/31 §3).

Profile-owned storage: SQL and database objects for asset state live in
this migration and the profile's PostgreSQL adapter only.  Tenant isolation
is enforced by RLS + FORCE like every other business relation; the runtime
role receives SELECT only and always through the tenant-scoped session.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "ws6_asset_risk_state"
down_revision: str | None = "ws6_claim_graph_binding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE asset_risk_asset_state (
            tenant_id TEXT NOT NULL,
            asset_ref TEXT NOT NULL,
            asset_class TEXT NOT NULL,
            exposure_amount BIGINT NOT NULL,
            currency TEXT NOT NULL,
            status TEXT NOT NULL,
            source_revision TEXT,
            observed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, asset_ref),
            CONSTRAINT asset_risk_asset_state_tenant_fk FOREIGN KEY (tenant_id)
                REFERENCES tenant (tenant_id),
            CONSTRAINT asset_risk_asset_state_ref_ck CHECK (
                asset_ref ~ '^asset\\.[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
            CONSTRAINT asset_risk_asset_state_exposure_ck CHECK (exposure_amount >= 0),
            CONSTRAINT asset_risk_asset_state_status_ck CHECK (
                status IN ('active', 'frozen', 'retired'))
        )
        """
    )
    op.execute(
        """
        CREATE POLICY asset_risk_asset_state_tenant_isolation ON asset_risk_asset_state
            USING (tenant_id = grove_active_tenant())
            WITH CHECK (tenant_id = grove_active_tenant())
        """
    )
    op.execute("ALTER TABLE asset_risk_asset_state ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE asset_risk_asset_state FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grove_runtime') THEN
                GRANT SELECT ON asset_risk_asset_state TO grove_runtime;
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grove_runtime') THEN
                REVOKE SELECT ON asset_risk_asset_state FROM grove_runtime;
            END IF;
        END
        $$
        """
    )
    op.execute("DROP TABLE IF EXISTS asset_risk_asset_state")
