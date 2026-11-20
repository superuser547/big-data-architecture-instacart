"""Allow failed discovery attempts without a dataset fingerprint."""

from alembic import op

revision = "0002_nullable_run_fingerprint"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "ingestion_runs",
        "dataset_fingerprint",
        schema="ops",
        existing_type=None,
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "ingestion_runs",
        "dataset_fingerprint",
        schema="ops",
        existing_type=None,
        nullable=False,
    )
