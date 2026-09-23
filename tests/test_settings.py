import pytest
from sqlalchemy import select

from grocery.db.models import Setting
from grocery.settings_store import FIELDS, SettingsError, effective, parse_and_save
from tests.conftest import csrf_of, login

FORM = {
    "cycle_start_day": "23", "monthly_reference_eur": "450", "timezone": "Europe/Amsterdam",
    "llm_monthly_budget_eur": "7.5", "confirmed_retention_days": "10", "unconfirmed_retention_days": "20",
    "deals_enabled": "on", "deals_alerts": "off", "deals_mine_min_pct": "15", "deals_notable_min_pct": "40",
    "deals_notable_min_eur": "2,50", "deals_categories": "huishouden, Drogisterij",
}


# --- effective(): defaults, overrides, and corrupted rows -------------------

def test_effective_falls_back_to_the_env_settings_when_nothing_is_overridden(db, client):
    eff = effective(db, client.app.state.settings)
    assert eff.cycle_start_day == 1
    assert eff.monthly_reference_eur == client.app.state.settings.monthly_reference_eur
    assert eff.timezone == client.app.state.settings.timezone
    assert eff.llm_monthly_budget_eur == client.app.state.settings.llm_monthly_budget_eur
    assert eff.confirmed_retention_days == client.app.state.settings.confirmed_retention_days
    assert eff.unconfirmed_retention_days == client.app.state.settings.unconfirmed_retention_days


def test_effective_uses_a_saved_row_over_the_env_default(db, client):
    db.add(Setting(key="app.cycle_start_day", value="23"))
    db.commit()
    assert effective(db, client.app.state.settings).cycle_start_day == 23


def test_effective_ignores_an_unknown_or_corrupted_row(db, client):
    db.add(Setting(key="app.cycle_start_day", value="not a number"))
    db.add(Setting(key="app.something_removed", value="x"))
    db.commit()
    assert effective(db, client.app.state.settings).cycle_start_day == 1  # falls back, does not crash


def test_a_row_outside_the_app_prefix_is_not_touched(db, client):
    # the Turkish price row is seeded at startup; reading the settings must neither choke on it nor change it
    effective(db, client.app.state.settings)
    assert db.get(Setting, "quickadd.turkish.price_per_kg_cents").value == "849"


# --- parse_and_save(): validation --------------------------------------------

def test_parse_and_save_writes_every_field_and_effective_reads_it_back(db, client):
    eff = parse_and_save(db, client.app.state.settings, FORM)
    assert (eff.cycle_start_day, eff.monthly_reference_eur, eff.timezone) == (23, 450.0, "Europe/Amsterdam")
    assert (eff.llm_monthly_budget_eur, eff.confirmed_retention_days, eff.unconfirmed_retention_days) == (7.5, 10, 20)
    assert effective(db, client.app.state.settings) == eff


def test_parse_and_save_accepts_a_comma_decimal(db, client):
    eff = parse_and_save(db, client.app.state.settings, {**FORM, "monthly_reference_eur": "450,50"})
    assert eff.monthly_reference_eur == 450.5


def test_saving_again_updates_the_existing_rows_instead_of_duplicating(db, client):
    parse_and_save(db, client.app.state.settings, FORM)
    parse_and_save(db, client.app.state.settings, {**FORM, "cycle_start_day": "5"})
    rows = db.scalars(select(Setting).where(Setting.key == "app.cycle_start_day")).all()
    assert len(rows) == 1 and rows[0].value == "5"


@pytest.mark.parametrize("key,bad,message", [
    ("cycle_start_day", "0", "between"), ("cycle_start_day", "29", "between"), ("cycle_start_day", "x", "valid number"),
    ("monthly_reference_eur", "-1", "between"), ("llm_monthly_budget_eur", "-1", "between"),
    ("confirmed_retention_days", "0", "between"), ("unconfirmed_retention_days", "400", "between"),
    ("timezone", "Mars/Olympus", "timezone"), ("timezone", "", "enter a value"),
])
def test_out_of_range_or_invalid_values_are_rejected(db, client, key, bad, message):
    with pytest.raises(SettingsError, match=message):
        parse_and_save(db, client.app.state.settings, {**FORM, key: bad})


def test_nothing_is_saved_when_one_field_is_invalid(db, client):
    with pytest.raises(SettingsError):
        parse_and_save(db, client.app.state.settings, {**FORM, "cycle_start_day": "99"})
    assert db.scalar(select(Setting).where(Setting.key.like("app.%"))) is None


def test_every_field_has_a_label_and_a_kind():
    for f in FIELDS:
        assert f.label and f.kind in ("int", "float", "str", "choice")


def test_the_deal_settings_are_saved_in_canonical_form(db, client):
    eff = parse_and_save(db, client.app.state.settings, FORM)
    assert (eff.deals_enabled, eff.deals_alerts) == ("on", "off")
    assert (eff.deals_mine_min_pct, eff.deals_notable_min_pct, eff.deals_notable_min_eur) == (15, 40, 2.5)
    assert eff.deals_categories == "huishouden,drogisterij"  # trimmed, lower-cased


def test_deal_categories_may_be_empty_to_switch_that_alert_off(db, client):
    eff = parse_and_save(db, client.app.state.settings, {**FORM, "deals_categories": ""})
    assert eff.deals_categories == ""


@pytest.mark.parametrize("key,bad,message", [
    ("deals_enabled", "maybe", "choose on or off"), ("deals_alerts", "", "enter a value"),
    ("deals_categories", "huishouden,speelgoed", "unknown category speelgoed"),
    ("deals_notable_min_pct", "5", "between"), ("deals_mine_min_pct", "0", "between"),
])
def test_invalid_deal_settings_are_rejected(db, client, key, bad, message):
    with pytest.raises(SettingsError, match=message):
        parse_and_save(db, client.app.state.settings, {**FORM, key: bad})


def test_a_corrupted_choice_row_falls_back_to_the_default(db, client):
    db.add(Setting(key="app.deals_enabled", value="perhaps"))
    db.commit()
    assert effective(db, client.app.state.settings).deals_enabled == "on"


# --- the /settings page -------------------------------------------------------

def test_settings_requires_a_session(client):
    assert client.get("/settings", headers={"accept": "text/html"}).status_code == 303
    assert client.post("/settings").status_code == 401


def test_the_page_shows_the_current_values_and_a_link_in_the_menu(client):
    login(client)
    page = client.get("/settings").text
    assert 'name="cycle_start_day"' in page and 'value="1"' in page
    assert "/settings" in client.get("/").text


def test_saving_shows_a_confirmation_and_persists(client, db):
    login(client)
    resp = client.post("/settings", data={"csrf_token": csrf_of(client), **FORM})
    assert resp.status_code == 303
    page = client.get(resp.headers["location"]).text
    assert "Saved" in page and 'value="450"' in page
    assert effective(db, client.app.state.settings).cycle_start_day == 23


def test_an_invalid_value_rerenders_with_the_message_and_keeps_what_was_typed(client):
    login(client)
    resp = client.post("/settings", data={"csrf_token": csrf_of(client), **FORM, "cycle_start_day": "99"})
    assert resp.status_code == 422
    assert "between" in resp.text and 'value="99"' in resp.text


def test_saving_needs_the_csrf_token(client):
    login(client)
    resp = client.post("/settings", data=FORM)
    assert resp.status_code == 403


# --- effects elsewhere in the app --------------------------------------------

def test_changing_the_cycle_start_day_changes_the_monthly_overview(client, db):
    from datetime import date

    from tests.test_analytics_routes import make_receipt

    login(client)
    db.add(Setting(key="app.cycle_start_day", value="23"))
    db.commit()
    make_receipt(db, "plus", date(2026, 9, 20), [("item", 500, "Zuivel & eieren", "Kaas")])
    page = client.get("/overview?month=2026-09").text
    assert "23 Sep" in page  # the cycle range subtitle, since it no longer starts on day 1


def test_a_very_low_budget_stops_new_claude_calls(client, db):
    from grocery.jobs.handlers import process_one
    from tests.fakes import FakeShelfReader, shelf_label
    from tests.test_capture_routes import upload

    login(client)
    db.add(Setting(key="app.llm_monthly_budget_eur", value="0"))
    db.commit()
    resp = upload(client, csrf_of(client))
    process_one(db, client.app.state.settings, lambda s: None, None,
                shelf_reader_factory=lambda s: FakeShelfReader(shelf_label()))
    db.expire_all()
    page = client.get(f"/capture/{resp.json()['id']}").text
    assert "budget" in page.lower()


def test_a_shorter_retention_takes_effect_on_the_next_confirm(client, db):
    from datetime import timedelta

    from grocery.db.models import ShelfCapture
    from tests.test_capture_routes import form_data, new_read_capture

    login(client)
    cid = new_read_capture(client, csrf_of(client), db)
    db.add(Setting(key="app.confirmed_retention_days", value="1"))
    db.commit()
    client.post(f"/capture/{cid}/confirm", data=form_data(client))
    db.expire_all()
    capture = db.get(ShelfCapture, cid)
    assert capture.file.delete_after - capture.confirmed_at < timedelta(days=2)
