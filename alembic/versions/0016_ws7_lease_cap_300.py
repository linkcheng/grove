"""Raise the protected SQL lease ceiling from 90s to 300s (WS-7).

Owner-approved 2026-08-26: real LLM generation latency regularly exceeded
the previous 90s ceiling, which blocked functional validation.  This
revision mirrors the Python-side raise of app.execution
MAX_LEASE_SECONDS (90.0 -> 300.0).  Correctness invariants are unchanged:
the invoke budget must still fit strictly inside lease minus margin, the
claim/heartbeat CAS identities are untouched, and the run -> command lock
order is preserved verbatim.

Exactly two live catalog definitions embed the ceiling guard.  Following
the 0012 live-patch pattern, each is patched by reading
pg_get_functiondef, pinning the current definition hash (fail closed on
any drift), applying the single needle replacement, verifying the
resulting hash, and only then executing the patched definition.  CREATE
OR REPLACE preserves the existing owner and ACL, so no grants change.
"""

from __future__ import annotations

from collections.abc import Sequence
from hashlib import sha256

from alembic import op

revision: str = "ws7_lease_cap_300"
down_revision: str | None = "ws6_domain_view_runtime_event"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_CEILING_NEEDLE = "p_lease_seconds > 90"
_NEW_CEILING_NEEDLE = "p_lease_seconds > 300"

# (signature, definition hash with the 90s ceiling, definition hash with
# the 300s ceiling).  The 90s hashes equal the pinned WS3_SCHEMA_CONTRACT
# entries at contract version ws3-execution-authority-v9.
_LEASE_GUARD_SIGNATURES = (
    (
        "public.grove_claim_run_command(text, text, text, double precision)",
        "ca835fe6064873712e036727a0785b11ddae6ac7f5e72862774fa0d9ca31b15f",
        "f0c6fc162e0740a99bbb03d982bd3f6e09956f2b9051f67bc90abf72ac8ce917",
    ),
    (
        (
            "public.grove_heartbeat_run_command_internal("
            "text, uuid, uuid, bigint, text, text, text, bigint, "
            "timestamp with time zone, double precision)"
        ),
        "6ad7c5a9681557b772749acaa781e16bfa1133e3e9592c865b4ff80847542c2a",
        "654bb3febcd2a98b2434f5dbb577c8225cc442d96f5f1962f3c23236a0c2f4dc",
    ),
)


def _patch_lease_ceiling(
    signature: str,
    expected_hash: str,
    target_hash: str,
    source_needle: str,
    target_needle: str,
) -> None:
    connection = op.get_bind()
    row = connection.exec_driver_sql(
        "SELECT pg_get_functiondef(to_regprocedure(%s))",
        (signature,),
    ).one_or_none()
    if row is None or row[0] is None:
        raise RuntimeError(f"required lease guard function is missing: {signature}")
    definition = str(row[0])
    if sha256(definition.encode()).hexdigest() != expected_hash:
        raise RuntimeError(f"live lease guard definition drift: {signature}")
    if definition.count(source_needle) != 1:
        raise RuntimeError(
            f"lease guard ceiling needle shape drift: {signature} "
            f"expected=1 actual={definition.count(source_needle)}"
        )
    patched = definition.replace(source_needle, target_needle)
    if sha256(patched.encode()).hexdigest() != target_hash:
        raise RuntimeError(f"patched lease guard hash mismatch: {signature}")
    connection.exec_driver_sql(patched.replace("%", "%%"))
    updated = connection.exec_driver_sql(
        "SELECT pg_get_functiondef(to_regprocedure(%s))",
        (signature,),
    ).scalar_one()
    if sha256(str(updated).encode()).hexdigest() != target_hash:
        raise RuntimeError(f"lease guard update did not persist: {signature}")


def upgrade() -> None:
    for signature, old_hash, new_hash in _LEASE_GUARD_SIGNATURES:
        _patch_lease_ceiling(signature, old_hash, new_hash, _OLD_CEILING_NEEDLE, _NEW_CEILING_NEEDLE)


def downgrade() -> None:
    for signature, old_hash, new_hash in _LEASE_GUARD_SIGNATURES:
        _patch_lease_ceiling(signature, new_hash, old_hash, _NEW_CEILING_NEEDLE, _OLD_CEILING_NEEDLE)
