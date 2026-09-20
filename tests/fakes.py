from io import BytesIO

from PIL import Image

from grocery.llm.client import ReadResult, Usage
from grocery.llm.schemas import ExtractedLine, ReceiptExtraction


def jpeg_bytes(color="white", size=(300, 400)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def line(kind="item", raw="PLUS HV MELK 1L", total=129, qty=1.0, unit="pcs", unit_price=None, name=None, cat=None):
    return ExtractedLine(
        kind=kind, raw_text=raw, quantity=qty, unit=unit, unit_price_cents=unit_price,
        line_total_cents=total, suggested_name=name, category=cat,
    )


def plus_receipt(total=846, date="2026-09-19", chain="plus", lines=None) -> ReceiptExtraction:
    return ReceiptExtraction(
        store_chain=chain,
        store_name_printed="PLUS Meggelaars",
        purchase_date=date,
        total_cents=total,
        lines=lines
        if lines is not None
        else [
            line(raw="PLUS HV MELK 1L", total=129, unit_price=129, name="Halfvolle melk 1L", cat="Zuivel & eieren"),
            line(raw="KIPFILET 0,874 KG", total=742, qty=0.874, unit="kg", unit_price=849, name="Kipfilet", cat="Vlees & vis"),
            line(kind="discount", raw="BONUS KIPFILET", total=-50),
            line(kind="deposit", raw="STATIEGELD", total=25),
        ],
        legibility_notes=None,
    )


class FakeReader:
    """Stands in for the Claude API. Pass a result, or a list to return one per call."""

    prompt_version = "receipt_test"
    model = "claude-sonnet-5"

    def __init__(self, result=None, tokens=(3000, 900)):
        self._results = result if isinstance(result, list) else [result]
        self._tokens = tokens
        self.calls = []

    def read(self, images, text):
        self.calls.append((len(images), text))
        item = self._results[min(len(self.calls) - 1, len(self._results) - 1)]
        if isinstance(item, ReadResult):
            return item
        return ReadResult(
            parsed=item,
            raw=item.model_dump() if item else None,
            model=self.model,
            usage=Usage(input_tokens=self._tokens[0], output_tokens=self._tokens[1]),
            request_id="req_test",
            latency_ms=1234,
        )


def failed(error, retryable=False) -> ReadResult:
    return ReadResult(parsed=None, raw=None, model="claude-sonnet-5", error=error, retryable=retryable)


# --- Phase 2: shelf labels and Open Food Facts ------------------------------

import json  # noqa: E402

from grocery.llm.schemas import ShelfLabelExtraction  # noqa: E402


def shelf_label(**kw) -> ShelfLabelExtraction:
    base = dict(
        product_name="Halfvolle melk", brand=None, price_cents=129, regular_price_cents=None,
        effective_price_cents=None, unit_price_cents=129, unit_price_per="l", promo_kind="none",
        promo_text=None, requires_card=False, valid_from=None, valid_until=None, ean_on_label=None,
        legibility_notes=None,
    )
    base.update(kw)
    return ShelfLabelExtraction(**base)


class FakeShelfReader(FakeReader):
    prompt_version = "shelf_test"


def ean13(body12: str) -> str:
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(body12))
    return body12 + str((10 - total % 10) % 10)


def off_found(name="Halfvolle melk", brand="Campina", quantity="1 L"):
    calls = []

    def fetch(ean, ua):
        calls.append((ean, ua))
        body = {"status": 1, "product": {"product_name": name, "brands": brand, "quantity": quantity}}
        return 200, json.dumps(body).encode()

    fetch.calls = calls
    return fetch


def off_missing():
    calls = []

    def fetch(ean, ua):
        calls.append(ean)
        return 404, b'{"status": 0, "status_verbose": "product not found"}'

    fetch.calls = calls
    return fetch


def off_down():
    def fetch(ean, ua):
        raise OSError("network unreachable")

    return fetch
