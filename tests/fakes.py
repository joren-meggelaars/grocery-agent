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
