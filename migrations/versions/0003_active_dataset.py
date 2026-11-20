"""Track the dataset currently represented by the core tables."""

from alembic import op

revision = "0003_active_dataset"
down_revision = "0002_nullable_run_fingerprint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ops.active_dataset (
            singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
            dataset_fingerprint VARCHAR(71) NOT NULL,
            run_id UUID NOT NULL REFERENCES ops.ingestion_runs(run_id),
            activated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE ops.active_dataset")
