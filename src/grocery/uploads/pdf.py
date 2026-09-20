"""PDF receipts.

PDF parsers have a large attack surface, so rendering happens in a child process with a
timeout and (on Linux) CPU and memory limits, and only page images and text come back.
Run as a module (`python -m grocery.uploads.pdf`) it is the child: PDF bytes on stdin,
one JSON document on stdout.
"""

import base64
import json
import subprocess
import sys
from dataclasses import dataclass
from io import BytesIO

from grocery.uploads.images import MAX_LONG_EDGE, UploadError

MAX_PAGES = 6
TIMEOUT_SECONDS = 30
TEXT_LAYER_MIN_CHARS = 200  # below this the PDF is treated as a scan and read visually


@dataclass(frozen=True)
class PdfContent:
    pages: list[bytes]  # one JPEG per page
    text: str  # concatenated text layer; empty for scans

    @property
    def has_text_layer(self) -> bool:
        return len(self.text.strip()) >= TEXT_LAYER_MIN_CHARS


def _limits() -> None:  # pragma: no cover - runs in the child, Linux only
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (TIMEOUT_SECONDS, TIMEOUT_SECONDS))
    resource.setrlimit(resource.RLIMIT_AS, (2 << 30, 2 << 30))


def rasterize_pdf(data: bytes, max_pages: int = MAX_PAGES) -> PdfContent:
    kwargs = {"preexec_fn": _limits} if sys.platform != "win32" else {}
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "grocery.uploads.pdf", str(max_pages)],
            input=data,
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            **kwargs,
        )
    except subprocess.TimeoutExpired as exc:
        raise UploadError("The PDF took too long to read.") from exc
    try:
        result = json.loads(proc.stdout)
    except ValueError as exc:
        raise UploadError("That PDF could not be read.") from exc
    if not result.get("ok"):
        raise UploadError(result.get("error") or "That PDF could not be read.")
    pages = [base64.b64decode(p) for p in result["pages"]]
    return PdfContent(pages=pages, text=result.get("text", ""))


def _child(max_pages: int) -> dict:
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(sys.stdin.buffer.read())
    except Exception:
        return {"ok": False, "error": "That file is not a readable PDF."}
    count = len(pdf)
    if count == 0:
        return {"ok": False, "error": "The PDF has no pages."}
    if count > max_pages:
        return {"ok": False, "error": f"The PDF has {count} pages; the limit is {max_pages}."}
    pages, texts = [], []
    for index in range(count):
        page = pdf[index]
        width, height = page.get_size()
        if width <= 0 or height <= 0:
            return {"ok": False, "error": "The PDF has an invalid page size."}
        scale = min(2.0, MAX_LONG_EDGE / max(width, height))
        image = page.render(scale=scale).to_pil().convert("RGB")
        buf = BytesIO()
        image.save(buf, "JPEG", quality=85)
        pages.append(base64.b64encode(buf.getvalue()).decode())
        texts.append(page.get_textpage().get_text_range())
    return {"ok": True, "pages": pages, "text": "\n".join(texts)}


if __name__ == "__main__":
    try:
        out = _child(int(sys.argv[1]) if len(sys.argv) > 1 else MAX_PAGES)
    except Exception as exc:  # never leak a traceback to the parent's stdout
        out = {"ok": False, "error": f"That PDF could not be read ({type(exc).__name__})."}
    sys.stdout.write(json.dumps(out))
