from datetime import timedelta
from io import BytesIO

import pytest
from PIL import Image

from grocery.db.base import utcnow
from grocery.db.models import Receipt, ReceiptFile
from grocery.uploads import images
from grocery.uploads.images import UploadError, reencode_image, sniff_kind
from grocery.uploads.pdf import rasterize_pdf
from grocery.uploads.storage import (
    _resolve,
    purge_due_files,
    read_image,
    save_image,
    schedule_deletion,
)
from tests.conftest import login


def make_image(fmt="JPEG", size=(120, 80), mode="RGB", exif=None) -> bytes:
    buf = BytesIO()
    kwargs = {"exif": exif} if exif is not None else {}
    Image.new(mode, size, "white").save(buf, fmt, **kwargs)
    return buf.getvalue()


def image_pdf(pages=1) -> bytes:
    frames = [Image.new("RGB", (300, 400), "white") for _ in range(pages)]
    buf = BytesIO()
    frames[0].save(buf, "PDF", save_all=True, append_images=frames[1:])
    return buf.getvalue()


TEXT_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 400]/Contents 4 0 R"
    b"/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
    b"4 0 obj<</Length 1000>>stream\nBT /F1 10 Tf 10 380 Td (" + b"TOTAAL 12,34 EUR PLUS SUPERMARKT " * 12 + b") Tj ET\nendstream endobj\n"
    b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


# --- images -----------------------------------------------------------------

def test_sniff_detects_by_content_not_name():
    assert sniff_kind(make_image("JPEG")) == "image"
    assert sniff_kind(make_image("PNG")) == "image"
    assert sniff_kind(make_image("WEBP")) == "image"
    assert sniff_kind(b"%PDF-1.7 ...") == "pdf"
    assert sniff_kind(b"<html><script>alert(1)</script>") is None
    assert sniff_kind(b"MZ\x90\x00 executable") is None


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
def test_allowed_formats_become_jpeg(fmt):
    out = reencode_image(make_image(fmt))
    assert out.data[:3] == b"\xff\xd8\xff"
    assert (out.width, out.height) == (120, 80)
    assert len(out.sha256) == 64


def test_gif_is_rejected_even_though_pillow_can_open_it():
    with pytest.raises(UploadError, match="Unsupported"):
        reencode_image(make_image("GIF", mode="P"))


def test_text_or_html_renamed_to_jpg_is_rejected():
    with pytest.raises(UploadError):
        reencode_image(b"<html><body>not an image</body></html>")


def test_truncated_image_is_rejected():
    with pytest.raises(UploadError):
        reencode_image(make_image("JPEG", size=(400, 400))[:200])


def test_exif_including_gps_is_stripped():
    exif = Image.Exif()
    exif[0x010F] = "SecretCameraMaker"
    exif[0x8825] = {1: "N", 2: (52.0, 22.0, 0.0), 3: "E", 4: (4.0, 53.0, 0.0)}  # GPS IFD
    original = make_image("JPEG", exif=exif)
    assert b"SecretCameraMaker" in original
    out = reencode_image(original)
    assert b"SecretCameraMaker" not in out.data
    assert not Image.open(BytesIO(out.data)).getexif()


def test_exif_rotation_is_applied_before_it_is_dropped():
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 degrees when displayed
    out = reencode_image(make_image("JPEG", size=(120, 80), exif=exif))
    assert (out.width, out.height) == (80, 120)


def test_trailing_payload_after_the_image_is_removed():
    polyglot = make_image("JPEG") + b"PK\x03\x04 hidden archive payload"
    out = reencode_image(polyglot)
    assert b"hidden archive payload" not in out.data


def test_large_images_are_downscaled_to_the_long_edge_limit():
    out = reencode_image(make_image("JPEG", size=(4000, 3000)))
    assert max(out.width, out.height) == images.MAX_LONG_EDGE


def test_transparent_png_is_flattened_onto_white():
    buf = BytesIO()
    Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(buf, "PNG")
    out = reencode_image(buf.getvalue())
    assert Image.open(BytesIO(out.data)).getpixel((5, 5)) == (255, 255, 255)


def test_pixel_cap_is_enforced_from_the_header(monkeypatch):
    monkeypatch.setattr(images, "MAX_PIXELS", 1000)
    with pytest.raises(UploadError, match="too large"):
        reencode_image(make_image("PNG", size=(100, 100)))


# --- pdf --------------------------------------------------------------------

def test_scanned_pdf_is_rendered_and_has_no_text_layer():
    content = rasterize_pdf(image_pdf())
    assert len(content.pages) == 1
    assert content.pages[0][:3] == b"\xff\xd8\xff"
    assert not content.has_text_layer


def test_pdf_with_text_layer_is_detected():
    content = rasterize_pdf(TEXT_PDF)
    assert content.has_text_layer
    assert "TOTAAL 12,34" in content.text


def test_pdf_page_limit():
    with pytest.raises(UploadError, match="limit"):
        rasterize_pdf(image_pdf(pages=3), max_pages=2)


def test_garbage_pdf_is_rejected_cleanly():
    with pytest.raises(UploadError):
        rasterize_pdf(b"%PDF-1.4 this is not really a pdf")


# --- storage ----------------------------------------------------------------

def test_stored_files_use_random_names_and_round_trip(client, db):
    settings = client.app.state.settings
    processed = reencode_image(make_image())
    row = save_image(db, settings, processed, None)
    db.commit()
    assert row.path.endswith(".jpg") and "/" in row.path
    assert read_image(settings, row) == processed.data


def test_path_traversal_in_a_stored_path_is_refused(client):
    settings = client.app.state.settings
    with pytest.raises(ValueError):
        _resolve(settings, "../../etc/passwd")


def test_purge_removes_due_files_and_keeps_a_tombstone(client, db):
    settings = client.app.state.settings
    now = utcnow()
    due = save_image(db, settings, reencode_image(make_image(size=(50, 50))), now - timedelta(hours=1))
    later = save_image(db, settings, reencode_image(make_image(size=(60, 60))), now + timedelta(days=3))
    db.commit()
    due_path = _resolve(settings, due.path)
    assert due_path.exists()

    assert purge_due_files(db, settings, now) == 1
    assert not due_path.exists()
    assert due.path is None and due.deleted_at is not None
    assert read_image(settings, due) is None
    assert read_image(settings, later) is not None


def test_schedule_deletion_sets_the_deadline_for_a_receipts_files(client, db):
    settings = client.app.state.settings
    receipt = Receipt(status="needs_review", source="photo")
    db.add(receipt)
    db.flush()
    f = save_image(db, settings, reencode_image(make_image()), None)
    db.add(ReceiptFile(receipt_id=receipt.id, file_id=f.id, page_no=1))
    db.commit()
    when = utcnow() + timedelta(days=7)
    schedule_deletion(db, receipt.id, when)
    db.commit()
    assert f.delete_after == when


# --- request body limit -----------------------------------------------------

def test_oversized_request_is_rejected_before_it_reaches_the_app(make_client):
    client = make_client(max_request_bytes=1000)
    resp = client.post("/login", content=b"x" * 5000, headers={"content-type": "application/octet-stream"})
    assert resp.status_code == 413


def test_normal_requests_still_work_under_the_limit(make_client):
    client = make_client(max_request_bytes=100_000)
    assert login(client).status_code == 303
