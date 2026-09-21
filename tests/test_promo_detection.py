import pytest

from grocery.llm.client import SHELF_PROMPT_VERSION, system_prompt
from grocery.db.models import ShelfCapture
from tests.conftest import csrf_of, login
from tests.test_capture_routes import new_read_capture
from tests.test_capture_service import env, new_capture, work  # noqa: F401  (env is a fixture)
from tests.fakes import shelf_label


# --- a price under the crossed-out "van" price is a promotion, whatever the model said ----------

def test_a_lower_price_than_the_was_price_becomes_a_promotion_even_when_the_model_said_none(env):
    settings, db = env
    c = new_capture(env)
    work(env, shelf_label(product_name="Pastinaak", price_cents=99, regular_price_cents=149, promo_kind="none", promo_text=None))
    db.refresh(c)
    assert c.promo_kind == "fixed_price"
    assert c.promo_text == "Was EUR 1.49"
    assert c.price_cents == 99


def test_the_models_own_promotion_and_wording_are_kept(env):
    settings, db = env
    c = new_capture(env)
    work(env, shelf_label(price_cents=99, regular_price_cents=149, promo_kind="fixed_price", promo_text="van 1.49 voor 0.99"))
    db.refresh(c)
    assert (c.promo_kind, c.promo_text) == ("fixed_price", "van 1.49 voor 0.99")


def test_the_model_wording_is_kept_when_only_the_kind_was_missing(env):
    settings, db = env
    c = new_capture(env)
    work(env, shelf_label(price_cents=99, regular_price_cents=149, promo_kind="none", promo_text="Aanbieding"))
    db.refresh(c)
    assert (c.promo_kind, c.promo_text) == ("fixed_price", "Aanbieding")


@pytest.mark.parametrize("price,regular", [(149, None), (149, 149), (149, 99), (149, 0)])
def test_no_promotion_is_invented_without_a_higher_was_price(env, price, regular):
    settings, db = env
    c = new_capture(env)
    work(env, shelf_label(price_cents=price, regular_price_cents=regular, promo_kind="none"))
    db.refresh(c)
    assert c.promo_kind is None and c.promo_text is None


# --- the prompt asks for it explicitly ------------------------------------------------------------

def test_the_shelf_prompt_is_version_two_and_names_the_promotion_signals():
    assert SHELF_PROMPT_VERSION == "shelf_v2"
    prompt = system_prompt(SHELF_PROMPT_VERSION)
    for word in ("actie", "aanbieding", "crossed-out", "NEVER"):
        assert word in prompt


def test_the_old_prompt_file_is_still_there_for_old_extractions():
    assert "promo_kind" in system_prompt("shelf_v1")


# --- the raw reader answer is visible on the detail page -------------------------------------------

def test_the_capture_page_shows_what_the_reader_returned_and_escapes_it(client, db):
    login(client)
    token = csrf_of(client)
    cid = new_read_capture(client, token, db, label=shelf_label(
        product_name="Pastinaak", promo_text="</pre><script>alert(1)</script>", promo_kind="other"))
    page = client.get(f"/capture/{cid}").text
    assert "What the reader returned" in page and '"price_cents": 129' in page
    assert "<script>alert" not in page and "</pre><script>" not in page


def test_the_raw_answer_is_not_shown_while_the_label_is_still_being_read(client):
    from tests.test_capture_routes import upload

    login(client)
    cid = upload(client, csrf_of(client)).json()["id"]
    assert "What the reader returned" not in client.get(f"/capture/{cid}").text


def test_a_capture_row_exists_for_the_derived_promotion_in_the_ui(client, db):
    login(client)
    cid = new_read_capture(client, csrf_of(client), db, label=shelf_label(
        product_name="Pastinaak", price_cents=99, regular_price_cents=149, promo_kind="none"))
    capture = db.get(ShelfCapture, cid)
    assert capture.promo_kind == "fixed_price"
    assert "Was EUR 1.49" in client.get(f"/capture/{cid}").text
