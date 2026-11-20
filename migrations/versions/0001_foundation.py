"""Create the normalized core and ingestion operations schemas."""

from alembic import op

revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA core")
    op.execute("CREATE SCHEMA ops")
    op.execute("CREATE SCHEMA mart")

    op.execute(
        """
        CREATE TABLE core.users (
            user_id INTEGER PRIMARY KEY CHECK (user_id > 0)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE core.departments (
            department_id SMALLINT PRIMARY KEY CHECK (department_id > 0),
            department_name TEXT NOT NULL UNIQUE CHECK (length(trim(department_name)) > 0)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE core.aisles (
            aisle_id SMALLINT PRIMARY KEY CHECK (aisle_id > 0),
            aisle_name TEXT NOT NULL UNIQUE CHECK (length(trim(aisle_name)) > 0),
            department_id SMALLINT NOT NULL REFERENCES core.departments(department_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE core.products (
            product_id INTEGER PRIMARY KEY CHECK (product_id > 0),
            product_name TEXT NOT NULL CHECK (length(trim(product_name)) > 0),
            aisle_id SMALLINT NOT NULL REFERENCES core.aisles(aisle_id)
        )
        """
    )
    op.execute("CREATE INDEX idx_products_aisle_id ON core.products (aisle_id)")
    op.execute(
        """
        CREATE TABLE core.orders (
            order_id INTEGER PRIMARY KEY CHECK (order_id > 0),
            user_id INTEGER NOT NULL REFERENCES core.users(user_id),
            eval_set VARCHAR(8) NOT NULL CHECK (eval_set IN ('prior', 'train', 'test')),
            order_number SMALLINT NOT NULL CHECK (order_number BETWEEN 1 AND 32767),
            order_dow SMALLINT NOT NULL CHECK (order_dow BETWEEN 0 AND 6),
            order_hour_of_day SMALLINT NOT NULL CHECK (order_hour_of_day BETWEEN 0 AND 23),
            days_since_prior_order REAL CHECK (days_since_prior_order BETWEEN 0 AND 30),
            CONSTRAINT uq_orders_user_order_number UNIQUE (user_id, order_number),
            CONSTRAINT ck_orders_first_order_days CHECK (
                (order_number = 1 AND days_since_prior_order IS NULL)
                OR (order_number > 1 AND days_since_prior_order IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE core.order_items (
            order_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            add_to_cart_order SMALLINT NOT NULL CHECK (add_to_cart_order > 0),
            reordered BOOLEAN NOT NULL,
            CONSTRAINT pk_order_items PRIMARY KEY (order_id, product_id),
            CONSTRAINT uq_order_items_cart_position UNIQUE (order_id, add_to_cart_order),
            CONSTRAINT fk_order_items_order FOREIGN KEY (order_id)
                REFERENCES core.orders(order_id),
            CONSTRAINT fk_order_items_product FOREIGN KEY (product_id)
                REFERENCES core.products(product_id)
        )
        """
    )
    op.execute("CREATE INDEX idx_order_items_product_id ON core.order_items (product_id)")
    op.execute(
        """
        CREATE TABLE ops.ingestion_runs (
            run_id UUID PRIMARY KEY,
            dataset_fingerprint VARCHAR(71),
            status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
            started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            finished_at TIMESTAMPTZ,
            row_counts JSONB NOT NULL DEFAULT '{}'::jsonb,
            validation_results JSONB NOT NULL DEFAULT '{}'::jsonb,
            error_message TEXT,
            CHECK ((status = 'running' AND finished_at IS NULL)
                OR (status <> 'running' AND finished_at IS NOT NULL))
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_ingestion_runs_fingerprint_status ON ops.ingestion_runs "
        "(dataset_fingerprint, status, finished_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP SCHEMA mart CASCADE")
    op.execute("DROP SCHEMA ops CASCADE")
    op.execute("DROP SCHEMA core CASCADE")
