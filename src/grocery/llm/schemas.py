"""What the model must return for a receipt. Every field is required (nullable where a value
may be missing) so the JSON schema stays simple for constrained decoding."""

from typing import Literal

from pydantic import BaseModel, Field

Chain = Literal["plus", "jumbo", "lidl", "aldi", "ah", "bakery", "turkish", "other", "unknown"]
LineKind = Literal["item", "discount", "deposit", "bag", "rounding"]
Unit = Literal["pcs", "kg", "l"]


class ExtractedLine(BaseModel):
    kind: LineKind
    raw_text: str = Field(description="The line exactly as printed, abbreviations included")
    quantity: float = Field(description="Piece count, or weight in kg / volume in l for weighed goods")
    unit: Unit
    unit_price_cents: int | None = Field(description="Price of one unit as printed, in cents")
    line_total_cents: int = Field(description="Amount for the whole line in cents; negative for discounts")
    suggested_name: str | None = Field(description="Short clean Dutch product name, same on any receipt")
    category: str | None


PromoKind = Literal["none", "percentage", "multi_buy", "x_for_y", "fixed_price", "bonus_card", "other"]
UnitPer = Literal["kg", "l", "pcs", "100g", "100ml"]


class ShelfLabelExtraction(BaseModel):
    """A supermarket shelf price label (schapkaartje)."""

    product_name: str | None
    brand: str | None
    price_cents: int | None = Field(description="Price of one pack on the shelf now, in cents")
    regular_price_cents: int | None = Field(description="The normal price when a promotion is shown, in cents")
    effective_price_cents: int | None = Field(
        description="Price per item once the promotion conditions are met, in cents; null without a promotion"
    )
    unit_price_cents: int | None = Field(description="The printed price per kg / l / piece, in cents")
    unit_price_per: UnitPer | None
    promo_kind: PromoKind
    promo_text: str | None = Field(description="The promotion exactly as printed, e.g. '2 voor 5.00'")
    requires_card: bool = Field(description="True if the price needs a loyalty card or app")
    valid_from: str | None = Field(description="YYYY-MM-DD")
    valid_until: str | None = Field(description="YYYY-MM-DD")
    ean_on_label: str | None = Field(description="Barcode digits printed on the label, if any")
    legibility_notes: str | None


class ReceiptExtraction(BaseModel):
    store_chain: Chain
    store_name_printed: str | None
    purchase_date: str | None = Field(description="YYYY-MM-DD")
    total_cents: int | None = Field(description="Amount to pay, in cents")
    lines: list[ExtractedLine]
    legibility_notes: str | None
