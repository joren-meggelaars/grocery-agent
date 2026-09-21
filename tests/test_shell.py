import re
from pathlib import Path

import pytest

from tests.conftest import login

STATIC = Path(__file__).resolve().parent.parent / "src/grocery/web/static"


def active_links(html: str) -> list[str]:
    """Hrefs of the sidebar items marked as the current page."""
    return re.findall(r'class="nav-link active" href="([^"]+)"', html)


@pytest.fixture
def session(client):
    login(client)
    return client


def test_login_uses_the_auth_layout_without_navigation(client):
    html = client.get("/login").text
    assert "auth-shell" in html and 'class="sidebar"' not in html and 'class="tabbar"' not in html
    assert 'name="username"' in html and 'name="password"' in html


@pytest.mark.parametrize(
    "path,href",
    [
        ("/", "/"),
        ("/capture", "/capture"),
        ("/capture/queue", "/capture/queue"),
        ("/receipts", "/receipts"),
        ("/receipts/new", "/receipts/new"),
        ("/receipts/quick?store=bakery", "/receipts/new"),
        ("/overview", "/overview"),
        ("/regulars", "/regulars"),
        ("/cupboard", "/cupboard"),
        ("/cupboard/scan", "/cupboard"),
        ("/account", "/account"),
    ],
)
def test_the_current_page_is_highlighted_in_the_sidebar(session, path, href):
    html = session.get(path, headers={"accept": "text/html"}).text
    assert active_links(html) == [href], path
    assert f'href="{href}" aria-current="page"' in html


def test_the_phone_layout_has_an_app_bar_a_tab_bar_and_a_menu_button(session):
    html = session.get("/").text
    assert 'class="appbar"' in html and 'class="tabbar"' in html
    assert html.count("data-nav-toggle") == 2  # app bar button and the "More" tab
    for label in ("Home", "Receipts", "Scan", "Overview", "More"):
        assert f">{label}</button>" in html or f"</span>{label}</a>" in html or f"</span>{label}</button>" in html
    assert 'class="tab-item scan' in html


def test_the_menu_toggle_script_exists():
    js = (STATIC / "js/common.js").read_text(encoding="utf-8")
    assert "data-nav-toggle" in js and "nav-open" in js


def test_sign_out_is_in_the_sidebar_only_where_a_csrf_token_exists(session):
    assert 'action="/logout"' in session.get("/").text
    scan = session.get("/capture").text  # the same page for everyone, so it carries no token and no logout form
    assert 'action="/logout"' not in scan and 'href="/account"' in scan


def test_shared_pages_show_no_user_specific_text(session):
    scan = session.get("/capture").text
    assert "joren" not in scan and "Signed in as" not in scan and 'name="csrf-token"' not in scan


def test_the_home_dashboard_leads_with_food_spend_against_the_reference(session):
    html = session.get("/").text
    assert "hero-card" in html and "Food spend in" in html and "€400.00 reference" in html
    for tile in ("Scan in store", "Add receipt", "Products at home", "What I buy most", "Bakery", "Turkish supermarket"):
        assert tile in html
    assert 'class="tile primary" href="/capture"' in html


def test_the_overview_uses_the_same_hero_card(session):
    html = session.get("/overview").text
    assert "hero-card" in html and 'class="figure"' in html


def test_theme_colour_favicon_and_manifest_are_black(client):
    html = client.get("/login").text
    assert '<meta name="theme-color" content="#070b14">' in html and 'rel="icon"' in html
    favicon = client.get("/static/favicon.svg")
    assert favicon.status_code == 200 and "svg" in favicon.headers["content-type"]
    manifest = client.get("/manifest.webmanifest").json()
    assert manifest["theme_color"] == "#070b14" and manifest["background_color"] == "#070b14"


def test_the_stylesheet_has_a_light_and_a_dark_scheme_and_a_hidden_rule():
    css = (STATIC / "css/app.css").read_text(encoding="utf-8")
    assert "prefers-color-scheme: dark" in css and "--hero:" in css and "--sidebar:" in css
    # elements with the hidden attribute must stay hidden even when a class sets display (the outbox bar did not)
    assert "[hidden] { display: none !important; }" in css


def test_the_stylesheet_only_uses_defined_colour_tokens():
    css = (STATIC / "css/app.css").read_text(encoding="utf-8")
    defined = set(re.findall(r"(--[a-z0-9-]+):", css))
    used = set(re.findall(r"var\((--[a-z0-9-]+)\)", css))
    assert used <= defined, f"undefined CSS variables: {sorted(used - defined)}"


def test_no_template_uses_the_removed_class_names(session):
    for path in ("/", "/overview", "/receipts", "/capture", "/cupboard", "/regulars"):
        html = session.get(path, headers={"accept": "text/html"}).text
        assert 'class="topbar"' not in html and 'class="hero"' not in html, path
