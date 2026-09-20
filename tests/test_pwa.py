import hashlib
import json
import re
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from tests.conftest import login

STATIC = Path(__file__).resolve().parent.parent / "src/grocery/web/static"


def test_service_worker_and_manifest_are_public_and_correctly_typed(client):
    sw = client.get("/sw.js")  # no login
    assert sw.status_code == 200 and sw.headers["content-type"].startswith("text/javascript")
    assert sw.headers["service-worker-allowed"] == "/"
    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200 and manifest.headers["content-type"].startswith("application/manifest+json")


def test_manifest_points_at_the_capture_page_and_real_icons(client):
    data = json.loads(client.get("/manifest.webmanifest").text)
    assert data["start_url"] == "/capture" and data["display"] == "standalone" and data["scope"] == "/"
    assert {i["sizes"] for i in data["icons"]} == {"192x192", "512x512"}
    for icon in data["icons"]:
        resp = client.get(icon["src"])
        assert resp.status_code == 200 and resp.headers["content-type"] == "image/png"
        width, height = Image.open(BytesIO(resp.content)).size
        assert f"{width}x{height}" == icon["sizes"]


def test_apple_touch_icon_and_manifest_are_linked_from_every_page(client):
    html = client.get("/login").text
    assert 'rel="manifest"' in html and 'rel="apple-touch-icon"' in html
    assert 'name="apple-mobile-web-app-capable"' in html
    icon = client.get("/static/icons/apple-touch-icon.png")
    assert Image.open(BytesIO(icon.content)).size == (180, 180)


def test_service_worker_only_handles_the_capture_page_and_static_files():
    sw = (STATIC / "sw.js").read_text()
    assert 'var PAGE = "/capture"' in sw and 'indexOf("/static/") === 0' in sw
    assert 'req.method !== "GET"' in sw  # posts (uploads) are never intercepted
    assert "/receipts" not in sw and "/account" not in sw  # nothing with personal data is cached


def test_vendored_barcode_library_matches_its_recorded_hash():
    directory = STATIC / "vendor/zxing"
    recorded = re.search(r"sha256: ([0-9a-f]{64})", (directory / "VERSION.txt").read_text()).group(1)
    actual = hashlib.sha256((directory / "zxing-library.min.js").read_bytes()).hexdigest()
    assert actual == recorded, "the vendored library changed: re-verify it against the npm tarball"
    assert (directory / "LICENSE.txt").stat().st_size > 1000


@pytest.mark.parametrize("path", ["/login"])
def test_public_pages_have_no_inline_script_or_handlers(client, path):
    assert_csp_clean(client.get(path).text)


def assert_csp_clean(html: str) -> None:
    """The CSP forbids inline scripts, inline handlers and style attributes: templates must not use them."""
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "inline <script>"
    assert not re.search(r"\son[a-z]+\s*=", html, re.I), "inline event handler"
    assert not re.search(r'\sstyle\s*=', html, re.I), "inline style attribute"
    assert not re.search(r"javascript:", html, re.I)


def test_rendered_pages_are_csp_clean(client, db):
    login(client)
    for path in ("/", "/receipts", "/receipts/new", "/receipts/quick?store=turkish", "/capture", "/capture/queue",
                 "/account", "/overview", "/regulars", "/cupboard", "/cupboard/scan", "/cupboard/name"):
        assert_csp_clean(client.get(path, headers={"accept": "text/html"}).text)


def test_static_javascript_never_writes_html_from_data():
    """innerHTML with server or user data would be an XSS hole; the only allowed use is the template clone."""
    for js in (STATIC / "js").glob("*.js"):
        text = js.read_text()
        assert "eval(" not in text and "document.write" not in text, js.name
        uses = re.findall(r"\.innerHTML\s*=", text)
        assert len(uses) <= (1 if js.name == "review.js" else 0), f"{js.name} assigns innerHTML"
