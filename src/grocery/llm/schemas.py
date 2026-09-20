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


class ReceiptExtraction(BaseModel):
    store_chain: Chain
    store_name_printed: str | None
    purchase_date: str | None = Field(description="YYYY-MM-DD")
    total_cents: int | None = Field(description="Amount to pay, in cents")
    lines: list[ExtractedLine]
    legibility_notes: str | None
