"""Receipts: stores, categories, products, name mappings, files, extractions, receipts, lines, jobs

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-20
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

CATEGORIES = [
    ("Groente & fruit", True),
    ("Zuivel & eieren", True),
    ("Vlees & vis", True),
    ("Brood & banket", True),
    ("Dranken", True),
    ("Snacks & zoet", True),
    ("Houdbaar", True),
    ("Diepvries", True),
    ("Huishouden & verzorging", False),
    ("Overig", True),
]
STORES = [
    ("plus", "Plus", "regular"),
    ("jumbo", "Jumbo", "regular"),
    ("lidl", "Lidl", "target"),
    ("aldi", "Aldi", "other"),
    ("ah", "Albert Heijn", "other"),
    ("bakery", "Bakery", "regular"),
    ("turkish", "Turkish supermarket", "baseline"),
    ("other", "Other", "other"),
]


def upgrade() -> None:
    stores = op.create_table(
        "stores",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chain", sa.String(32), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_stores"),
        sa.UniqueConstraint("chain", name="uq_stores_chain"),
    )
    categories = op.create_table(
        "categories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("counts_as_food", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_categories"),
        sa.UniqueConstraint("name", name="uq_categories_name"),
    )
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("name_key", sa.String(200), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], name="fk_products_category_id_categories"),
        sa.PrimaryKeyConstraint("id", name="pk_products"),
        sa.UniqueConstraint("name_key", name="uq_products_name_key"),
    )
    op.create_table(
        "name_mappings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chain", sa.String(32), nullable=False),
        sa.Column("raw_norm", sa.String(200), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("confirmed_count", sa.Integer(), nullable=False),
        sa.Column("last_confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], name="fk_name_mappings_product_id_products", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_name_mappings"),
        sa.UniqueConstraint("chain", "raw_norm", name="uq_name_mappings_chain_raw"),
    )
    op.create_index("ix_name_mappings_product_id", "name_mappings", ["product_id"])
    op.create_table(
        "files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("mime", sa.String(32), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delete_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_files"),
    )
    op.create_index("ix_files_sha256", "files", ["sha256"])
    op.create_index("ix_files_delete_after", "files", ["delete_after"])
    op.create_table(
        "extractions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=True),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(32), nullable=False),
        sa.Column("raw_json", JSONType, nullable=True),
        sa.Column("parsed_ok", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_est_eur", sa.Float(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_extractions"),
    )
    op.create_index("ix_extractions_created_at", "extractions", ["created_at"])
    op.create_table(
        "receipts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("store_id", sa.Integer(), nullable=True),
        sa.Column("purchased_on", sa.Date(), nullable=True),
        sa.Column("total_cents", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source", sa.String(8), nullable=False),
        sa.Column("sum_delta_cents", sa.Integer(), nullable=True),
        sa.Column("flags", JSONType, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("source_text", sa.Text(), nullable=True),
        sa.Column("extraction_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], name="fk_receipts_store_id_stores"),
        sa.ForeignKeyConstraint(
            ["extraction_id"], ["extractions.id"], name="fk_receipts_extraction_id_extractions"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_receipts"),
    )
    op.create_index("ix_receipts_purchased_on", "receipts", ["purchased_on"])
    op.create_index("ix_receipts_status", "receipts", ["status"])
    op.create_table(
        "receipt_files",
        sa.Column("receipt_id", sa.Integer(), nullable=False),
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["receipt_id"], ["receipts.id"], name="fk_receipt_files_receipt_id_receipts", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], name="fk_receipt_files_file_id_files"),
        sa.PrimaryKeyConstraint("receipt_id", "file_id", name="pk_receipt_files"),
    )
    op.create_table(
        "receipt_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("receipt_id", sa.Integer(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("raw_text", sa.String(255), nullable=False),
        sa.Column("quantity_milli", sa.Integer(), nullable=False),
        sa.Column("unit", sa.String(8), nullable=False),
        sa.Column("unit_price_cents", sa.Integer(), nullable=True),
        sa.Column("line_total_cents", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("suggested_name", sa.String(200), nullable=True),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column("match_source", sa.String(12), nullable=False),
        sa.Column("match_confidence", sa.Float(), nullable=True),
        sa.Column("edited", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["receipt_id"], ["receipts.id"], name="fk_receipt_lines_receipt_id_receipts", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], name="fk_receipt_lines_product_id_products"),
        sa.ForeignKeyConstraint(
            ["category_id"], ["categories.id"], name="fk_receipt_lines_category_id_categories"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_receipt_lines"),
    )
    op.create_index("ix_receipt_lines_receipt_id", "receipt_lines", ["receipt_id"])
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payload", JSONType, nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
    )
    op.create_index("ix_jobs_status_run_after", "jobs", ["status", "run_after"])

    op.bulk_insert(
        stores,
        [{"chain": c, "name": n, "role": r} for c, n, r in STORES],
    )
    op.bulk_insert(
        categories,
        [
            {"name": name, "counts_as_food": food, "sort_order": i}
            for i, (name, food) in enumerate(CATEGORIES, start=1)
        ],
    )
    # Baseline price prefilled in the manual quick-add for the Turkish supermarket (EUR 8.49/kg, 2026-09-20).
    settings = sa.table("settings", sa.column("key", sa.String), sa.column("value", sa.Text))
    op.bulk_insert(settings, [{"key": "quickadd.turkish.price_per_kg_cents", "value": "849"}])


def downgrade() -> None:
    op.execute("DELETE FROM settings WHERE key = 'quickadd.turkish.price_per_kg_cents'")
    for table in (
        "jobs",
        "receipt_lines",
        "receipt_files",
        "receipts",
        "extractions",
        "files",
        "name_mappings",
        "products",
        "categories",
        "stores",
    ):
        op.drop_table(table)
