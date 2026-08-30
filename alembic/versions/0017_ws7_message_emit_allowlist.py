"""Allow the message fact family through the runtime-event emit guard (WS-7).

The typed answer now travels as message started/delta/completed runtime
facts (see app/observation/facts.py), but the SECURITY DEFINER emit
validator keeps a closed SQL-side schema-ref allowlist that still ended at
domain-view-accepted -- every terminal emit batch containing an answer
message was rejected with 22023 and the run could not finish delivery.
Following the 0016 live-patch pattern: pin the current definition hash,
apply the single needle replacement, verify the new hash, then execute.
ACLs are preserved by CREATE OR REPLACE.
"""

from __future__ import annotations

from collections.abc import Sequence
from hashlib import sha256

from alembic import op

revision: str = "ws7_message_emit_allowlist"
down_revision: str | None = "ws7_lease_cap_300"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_TAIL = "'grove.runtime.domain-view-accepted.v1'\n                   )"
_NEW_TAIL = (
    "'grove.runtime.domain-view-accepted.v1',\n"
    "                       'grove.runtime.message-started.v1',\n"
    "                       'grove.runtime.message-delta.v1',\n"
    "                       'grove.runtime.message-completed.v1'\n"
    "                   )"
)

_SIGNATURE = "public.grove_emit_runtime_events(text, uuid, uuid, text, uuid, text, jsonb)"
_OLD_HASH = "eea4800a01787174419efcf9fc325376be9a306661f6e03d46541bc7e705e185"
_NEW_HASH = "07141c37213ecfd7f775eeb420fd8f95c4aacceac941f84e87977c0cf38a355e"


def _patch_emit_allowlist(expected_hash: str, target_hash: str, source_tail: str, target_tail: str) -> None:
    connection = op.get_bind()
    row = connection.exec_driver_sql(
        "SELECT pg_get_functiondef(to_regprocedure(%s))",
        (_SIGNATURE,),
    ).one_or_none()
    if row is None or row[0] is None:
        raise RuntimeError("required runtime-event emit guard is missing")
    definition = str(row[0])
    if sha256(definition.encode()).hexdigest() != expected_hash:
        raise RuntimeError("live runtime-event emit guard definition drift")
    if definition.count(source_tail) != 1:
        raise RuntimeError(
            f"emit allowlist tail shape drift: expected=1 actual={definition.count(source_tail)}"
        )
    patched = definition.replace(source_tail, target_tail)
    if sha256(patched.encode()).hexdigest() != target_hash:
        raise RuntimeError("patched emit allowlist hash mismatch")
    connection.exec_driver_sql(patched.replace("%", "%%"))
    updated = connection.exec_driver_sql(
        "SELECT pg_get_functiondef(to_regprocedure(%s))",
        (_SIGNATURE,),
    ).scalar_one()
    if sha256(str(updated).encode()).hexdigest() != target_hash:
        raise RuntimeError("emit allowlist update did not persist")


def upgrade() -> None:
    _patch_emit_allowlist(_OLD_HASH, _NEW_HASH, _OLD_TAIL, _NEW_TAIL)


def downgrade() -> None:
    _patch_emit_allowlist(_NEW_HASH, _OLD_HASH, _NEW_TAIL, _OLD_TAIL)
