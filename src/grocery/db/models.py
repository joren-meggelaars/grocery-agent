from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from grocery.db.base import Base, UTCDateTime, utcnow

JSONType = JSON().with_variant(JSONB(), "postgresql")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    # Tailscale login (e-mail) that maps to this user in TS_IDENTITY_MODE=sso.
    tailscale_login: Mapped[str | None] = mapped_column(String(255), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    sessions: Mapped[list["AuthSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class AuthSession(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    # SHA-256 of the cookie token; the token itself is never stored.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_secret: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    user_agent: Mapped[str | None] = mapped_column(String(255))

    user: Mapped[User] = relationship(back_populates="sessions")


class AuthEvent(Base):
    """Append-only log used for lockout decisions and auditing."""

    __tablename__ = "auth_events"
    __table_args__ = (Index("ix_auth_events_username_ts", "username", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    username: Mapped[str] = mapped_column(String(64))
    ip: Mapped[str | None] = mapped_column(String(64))
    # login_ok | login_fail | lockout | logout | unlock
    kind: Mapped[str] = mapped_column(String(24))
    detail: Mapped[str | None] = mapped_column(String(255))


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Store(Base):
    __tablename__ = "stores"

    id: Mapped[int] = mapped_column(primary_key=True)
    chain: Mapped[str] = mapped_column(String(32), unique=True)  # plus, jumbo, lidl, bakery, turkish, ...
    name: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16))  # regular | target | baseline | other


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    counts_as_food: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Product(Base):
    """Canonical product. name_key is the case-folded name, used for exact lookup."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    name_key: Mapped[str] = mapped_column(String(200), unique=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    category: Mapped[Category | None] = relationship()


class NameMapping(Base):
    """Learns receipt text -> product from the user's confirmations, per chain."""

    __tablename__ = "name_mappings"
    __table_args__ = (UniqueConstraint("chain", "raw_norm", name="uq_name_mappings_chain_raw"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chain: Mapped[str] = mapped_column(String(32))
    raw_norm: Mapped[str] = mapped_column(String(200))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    confirmed_count: Mapped[int] = mapped_column(Integer, default=1)
    last_confirmed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    product: Mapped[Product] = relationship()


class StoredFile(Base):
    """A re-encoded image. Deleted from disk after delete_after; the row stays as a tombstone."""

    __tablename__ = "files"

    id: Mapped[int] = mapped_column(primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    mime: Mapped[str] = mapped_column(String(32))
    bytes: Mapped[int] = mapped_column(Integer)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    path: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    delete_after: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Extraction(Base):
    """One model call: raw output, token usage and estimated cost."""

    __tablename__ = "extractions"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # receipt | shelf
    subject_id: Mapped[int | None] = mapped_column(Integer)
    model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(32))
    raw_json: Mapped[dict | None] = mapped_column(JSONType)
    parsed_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_est_eur: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    request_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class Receipt(Base):
    __tablename__ = "receipts"

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id"))
    purchased_on: Mapped[date | None] = mapped_column(Date, index=True)
    total_cents: Mapped[int | None] = mapped_column(Integer)
    # extracting | needs_review | confirmed | failed
    status: Mapped[str] = mapped_column(String(16), index=True)
    source: Mapped[str] = mapped_column(String(8))  # photo | pdf | manual
    sum_delta_cents: Mapped[int | None] = mapped_column(Integer)
    flags: Mapped[list | None] = mapped_column(JSONType)
    error: Mapped[str | None] = mapped_column(Text)
    source_text: Mapped[str | None] = mapped_column(Text)  # text layer of a digital PDF receipt
    extraction_id: Mapped[int | None] = mapped_column(ForeignKey("extractions.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    store: Mapped[Store | None] = relationship()
    extraction: Mapped[Extraction | None] = relationship()
    lines: Mapped[list["ReceiptLine"]] = relationship(
        back_populates="receipt", cascade="all, delete-orphan", order_by="ReceiptLine.line_no"
    )
    files: Mapped[list["ReceiptFile"]] = relationship(
        cascade="all, delete-orphan", order_by="ReceiptFile.page_no"
    )


class ReceiptFile(Base):
    __tablename__ = "receipt_files"

    receipt_id: Mapped[int] = mapped_column(
        ForeignKey("receipts.id", ondelete="CASCADE"), primary_key=True
    )
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id"), primary_key=True)
    page_no: Mapped[int] = mapped_column(Integer, default=1)

    file: Mapped[StoredFile] = relationship()


class ReceiptLine(Base):
    """The corrected result. The raw model output stays untouched in extractions.raw_json."""

    __tablename__ = "receipt_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("receipts.id", ondelete="CASCADE"), index=True)
    line_no: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(12))  # item | discount | deposit | bag | rounding
    raw_text: Mapped[str] = mapped_column(String(255))
    quantity_milli: Mapped[int] = mapped_column(Integer, default=1000)  # 1000 = 1 piece, 874 = 0.874 kg
    unit: Mapped[str] = mapped_column(String(8), default="pcs")  # pcs | kg | l
    unit_price_cents: Mapped[int | None] = mapped_column(Integer)
    line_total_cents: Mapped[int] = mapped_column(Integer)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    suggested_name: Mapped[str | None] = mapped_column(String(200))  # proposed new product name
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    match_source: Mapped[str] = mapped_column(String(12), default="none")  # mapping | fuzzy | manual | none
    match_confidence: Mapped[float | None] = mapped_column(Float)
    edited: Mapped[bool] = mapped_column(Boolean, default=False)

    receipt: Mapped[Receipt] = relationship(back_populates="lines")
    product: Mapped[Product | None] = relationship()


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status_run_after", "status", "run_after"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSONType)
    status: Mapped[str] = mapped_column(String(10), default="queued")  # queued | running | done | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    run_after: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class ProductEan(Base):
    """Barcode -> canonical product, learned when a scanned product is named and confirmed."""

    __tablename__ = "product_eans"

    ean: Mapped[str] = mapped_column(String(14), primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(8))  # scan | manual
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    product: Mapped[Product] = relationship()


class OffCache(Base):
    """Open Food Facts answers, including "not found", so each barcode is asked for at most once per TTL."""

    __tablename__ = "off_cache"

    ean: Mapped[str] = mapped_column(String(14), primary_key=True)
    found: Mapped[bool] = mapped_column(Boolean)
    payload: Mapped[dict | None] = mapped_column(JSONType)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ShelfCapture(Base):
    __tablename__ = "shelf_captures"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_uuid: Mapped[str] = mapped_column(String(36), unique=True)  # idempotency key from the phone
    ean: Mapped[str | None] = mapped_column(String(14), index=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id"))
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    file_id: Mapped[int | None] = mapped_column(ForeignKey("files.id"))
    # extracting | needs_review | confirmed | failed
    status: Mapped[str] = mapped_column(String(16), index=True)
    error: Mapped[str | None] = mapped_column(Text)
    extraction_id: Mapped[int | None] = mapped_column(ForeignKey("extractions.id"))
    off_name: Mapped[str | None] = mapped_column(String(300))  # what Open Food Facts calls this barcode

    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    product_name: Mapped[str | None] = mapped_column(String(200))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    price_cents: Mapped[int | None] = mapped_column(Integer)  # shelf price for one pack
    effective_price_cents: Mapped[int | None] = mapped_column(Integer)  # per item once the promotion applies
    unit_price_cents: Mapped[int | None] = mapped_column(Integer)  # per kg or l
    unit_basis: Mapped[str | None] = mapped_column(String(4))  # kg | l
    promo_kind: Mapped[str | None] = mapped_column(String(16))
    promo_text: Mapped[str | None] = mapped_column(String(200))
    requires_card: Mapped[bool] = mapped_column(Boolean, default=False)
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_until: Mapped[date | None] = mapped_column(Date)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    store: Mapped[Store | None] = relationship()
    file: Mapped[StoredFile | None] = relationship()
    extraction: Mapped[Extraction | None] = relationship()
    product: Mapped[Product | None] = relationship()


class PriceObservation(Base):
    """One observed price for a product at a store: from a receipt line or a shelf label."""

    __tablename__ = "price_observations"
    __table_args__ = (Index("ix_price_observations_product_observed", "product_id", "observed_on"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    ean: Mapped[str | None] = mapped_column(String(14))
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"))
    price_cents: Mapped[int] = mapped_column(Integer)  # what one pack / the weighed line costs
    unit_price_cents: Mapped[int | None] = mapped_column(Integer)
    unit_basis: Mapped[str | None] = mapped_column(String(4))  # kg | l
    is_promo: Mapped[bool] = mapped_column(Boolean, default=False)
    promo_text: Mapped[str | None] = mapped_column(String(200))
    requires_card: Mapped[bool] = mapped_column(Boolean, default=False)
    observed_on: Mapped[date] = mapped_column(Date)
    valid_until: Mapped[date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(12))  # receipt | shelf
    source_ref_id: Mapped[int | None] = mapped_column(Integer)  # receipt id or shelf capture id
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    store: Mapped[Store] = relationship()
    product: Mapped[Product | None] = relationship()
