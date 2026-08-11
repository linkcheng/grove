"""Materialize the consumed-provenance contract for already-deployed heads."""

from __future__ import annotations

import re
from collections.abc import Sequence
from hashlib import sha256

from alembic import op

revision: str = "ws3_consumed_provenance_compat"
down_revision: str | None = "ws4_authority_audit_emitters"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CLAIM_PROVENANCE_ASSIGNMENT = "consumed_provenance_kind = 'claim.v1',"
_PROVENANCE_WRITER_SIGNATURES = (
    (
        "public.grove_consume_run_command_internal("
        "text,uuid,uuid,bigint,text,text,text,bigint,timestamp with time zone)",
        2,
        "2e2a44b0a8182962dca2a56727de0943bd8fd6264de5e6cb48ecc47d2aa8155f",
        "afe7cb6fe0903ff597dffdd0da02e677e9e84d47cb4e5c1bc02fdef544e3db13",
    ),
    (
        "public.grove_reconcile_expired_run_command_internal(text,uuid)",
        1,
        "8eafc0e8c3eab0a96c380daf04f3f270f71a91f963c2c6e5fb208aa4b076a430",
        "62c7d6ef60331441a024e2a082a437bf44d01757b5ad4d450badc5071d069525",
    ),
    (
        "public.grove_finish_delivery("
        "text,uuid,uuid,bigint,text,text,text,bigint,timestamp with time zone,text,text,text,jsonb)",
        1,
        "e9372922a3a6209b9e18a12238b3a559d5194562e42d5aa8c588f07e08ce278f",
        "3f5c15a9a70dbce72c076d3250919cdfed8a81cc97a55834aedd15e4d387f7c7",
    ),
)


def _patch_provenance_writer(
    signature: str,
    expected_assignments: int,
    old_definition_hash: str,
    new_definition_hash: str,
) -> None:
    connection = op.get_bind()
    row = connection.exec_driver_sql(
        "SELECT pg_get_functiondef(to_regprocedure(%s))",
        (signature,),
    ).one_or_none()
    if row is None or row[0] is None:
        raise RuntimeError(f"required provenance writer is missing: {signature}")
    definition = str(row[0])
    definition_hash = sha256(definition.encode()).hexdigest()
    existing_assignments = definition.count(_CLAIM_PROVENANCE_ASSIGNMENT)
    if existing_assignments == expected_assignments:
        if definition_hash != new_definition_hash:
            raise RuntimeError(f"current provenance writer definition drift: {signature}")
        return
    if existing_assignments != 0:
        raise RuntimeError(
            f"provenance writer has an unexpected discriminator shape: {signature} "
            f"expected=0-or-{expected_assignments} actual={existing_assignments}"
        )
    if definition_hash != old_definition_hash:
        raise RuntimeError(f"published provenance writer definition drift: {signature}")

    def add_assignment(match: re.Match[str]) -> str:
        indent = match.group("indent")
        return (
            f"{indent}SET status = 'consumed',\n"
            f"{indent}    {_CLAIM_PROVENANCE_ASSIGNMENT}\n"
        )

    patched, replacement_count = re.subn(
        r"(?m)^(?P<indent>[ \t]*)SET status = 'consumed',\n",
        add_assignment,
        definition,
    )
    if replacement_count != expected_assignments:
        raise RuntimeError(
            f"provenance writer update shape drift: {signature} "
            f"expected={expected_assignments} actual={replacement_count}"
        )
    connection.exec_driver_sql(patched.replace("%", "%%"))
    updated = connection.exec_driver_sql(
        "SELECT pg_get_functiondef(to_regprocedure(%s))",
        (signature,),
    ).scalar_one()
    updated_definition = str(updated)
    if (
        updated_definition.count(_CLAIM_PROVENANCE_ASSIGNMENT) != expected_assignments
        or sha256(updated_definition.encode()).hexdigest() != new_definition_hash
    ):
        raise RuntimeError(f"provenance writer update did not persist: {signature}")


def upgrade() -> None:
    op.execute("ALTER TABLE run_command ADD COLUMN IF NOT EXISTS consumed_provenance_kind TEXT")
    op.execute("ALTER TABLE run_command DROP CONSTRAINT IF EXISTS run_command_consumed_provenance_ck")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                  FROM public.run_command
                 WHERE status = 'consumed'
                   AND (
                        NOT (
                            (
                                consumed_worker_id IS NOT NULL
                                AND consumed_execution_fence IS NOT NULL
                                AND consumed_lease_until IS NOT NULL
                                AND consumed_claim_provenance_hash ~ '^[0-9a-f]{64}$'
                            )
                            OR (
                                consumed_worker_id IS NULL
                                AND consumed_execution_fence IS NULL
                                AND consumed_lease_until IS NULL
                                AND consumed_claim_provenance_hash IS NULL
                            )
                        )
                        OR (
                            consumed_worker_id IS NOT NULL
                            AND consumed_provenance_kind IS NOT NULL
                            AND consumed_provenance_kind <> 'claim.v1'
                        )
                        OR (
                            consumed_worker_id IS NULL
                            AND consumed_provenance_kind IS NOT NULL
                            AND consumed_provenance_kind <> 'legacy_unverified'
                        )
                   )
            ) THEN
                RAISE EXCEPTION 'consumed command provenance shape cannot be classified safely';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM public.run_command
                 WHERE status <> 'consumed'
                   AND (
                        consumed_provenance_kind IS NOT NULL
                        OR consumed_worker_id IS NOT NULL
                        OR consumed_execution_fence IS NOT NULL
                        OR consumed_lease_until IS NOT NULL
                        OR consumed_claim_provenance_hash IS NOT NULL
                   )
            ) THEN
                RAISE EXCEPTION 'non-consumed command carries consumed provenance';
            END IF;
        END
        $$
        """
    )
    op.execute(
        """
        UPDATE public.run_command
           SET consumed_provenance_kind = CASE
               WHEN consumed_worker_id IS NOT NULL THEN 'claim.v1'
               ELSE 'legacy_unverified'
           END
         WHERE status = 'consumed'
        """
    )
    op.execute(
        """
        ALTER TABLE run_command ADD CONSTRAINT run_command_consumed_provenance_ck CHECK (
            (status = 'consumed' AND consumed_provenance_kind IS NOT NULL
             AND consumed_provenance_kind = 'claim.v1'
             AND consumed_worker_id IS NOT NULL
             AND consumed_execution_fence IS NOT NULL
             AND consumed_lease_until IS NOT NULL
             AND consumed_claim_provenance_hash ~ '^[0-9a-f]{64}$')
            OR
            (status = 'consumed' AND consumed_provenance_kind IS NOT NULL
             AND consumed_provenance_kind = 'legacy_unverified'
             AND consumed_worker_id IS NULL
             AND consumed_execution_fence IS NULL
             AND consumed_lease_until IS NULL
             AND consumed_claim_provenance_hash IS NULL)
            OR
            (status <> 'consumed' AND consumed_provenance_kind IS NULL
             AND consumed_worker_id IS NULL
             AND consumed_execution_fence IS NULL
             AND consumed_lease_until IS NULL
             AND consumed_claim_provenance_hash IS NULL)
        )
        """
    )
    for signature, expected_assignments, old_definition_hash, new_definition_hash in _PROVENANCE_WRITER_SIGNATURES:
        _patch_provenance_writer(
            signature,
            expected_assignments,
            old_definition_hash,
            new_definition_hash,
        )


def downgrade() -> None:
    # This compatibility revision materializes the schema already defined by
    # the corrected historical migrations.  A no-op keeps a one-step downgrade
    # consistent with rebuilding that revision from current source; downgrade
    # to base still removes the column and functions in their owning revisions.
    pass
