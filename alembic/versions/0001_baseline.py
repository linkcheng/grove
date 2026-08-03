"""Create the WS-0 migration baseline without business tables."""

from collections.abc import Sequence

revision: str = "baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Reserve the migration graph; Contract Spine owns future tables."""


def downgrade() -> None:
    """Return to base; no WS-0 business objects exist to remove."""
