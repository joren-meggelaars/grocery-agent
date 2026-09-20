import pytest

from grocery.db.models import Category, NameMapping, Product
from grocery.products.matching import MatchIndex
from grocery.products.normalize import normalize_raw, product_key


def _product(db, name, category="Zuivel & eieren"):
    cat = db.query(Category).filter_by(name=category).one_or_none()
    p = Product(name=name, name_key=product_key(name), category_id=cat.id if cat else None)
    db.add(p)
    db.flush()
    return p


def _map(db, chain, raw, product):
    db.add(NameMapping(chain=chain, raw_norm=normalize_raw(raw), product_id=product.id))
    db.commit()


# --- normalisation ----------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    ["HALFVOLLE MELK 1L", "Halfvolle Melk 1L   1,29", "2 x halfvolle melk 1l", "halfvolle-melk 1L!", "HÁLFVOLLE MELK 1L 2.58"],
)
def test_normalisation_ignores_case_accents_prices_and_quantity_prefix(raw):
    assert normalize_raw(raw) == "halfvolle melk 1l"


def test_normalisation_keeps_sizes_that_distinguish_products():
    assert normalize_raw("MELK 1L") != normalize_raw("MELK 2L")


def test_empty_and_symbol_only_text_normalise_to_nothing():
    assert normalize_raw("  *** 1,29 ") == ""


# --- matching ---------------------------------------------------------------

def test_exact_learned_mapping_wins_and_is_per_chain(client, db):
    melk = _product(db, "Halfvolle melk 1L")
    _map(db, "plus", "PLUS HV MELK 1L", melk)
    idx = MatchIndex(db)

    hit = idx.find("plus", "plus hv melk 1l   1,29", None)
    assert (hit.source, hit.product_id, hit.confidence) == ("mapping", melk.id, 1.0)
    assert hit.category_id == melk.category_id

    # the same text at another chain is not an exact hit (only a very close fuzzy one)
    other = idx.find("jumbo", "PLUS HV MELK 1L", None)
    assert other.source == "fuzzy"


def test_fuzzy_match_against_learned_text_for_the_same_chain(client, db):
    melk = _product(db, "Halfvolle melk 1L")
    _map(db, "jumbo", "JUMBO HALFVOLLE MELK 1L", melk)
    hit = MatchIndex(db).find("jumbo", "JUMBO HALFVOLLE MELK 1 L", None)
    assert hit.source == "fuzzy" and hit.product_id == melk.id
    assert 0.88 <= hit.confidence < 1.0


def test_unrelated_text_does_not_match(client, db):
    melk = _product(db, "Halfvolle melk 1L")
    _map(db, "jumbo", "JUMBO HALFVOLLE MELK 1L", melk)
    hit = MatchIndex(db).find("jumbo", "KIPFILET 500G", None)
    assert hit.source == "none" and hit.product_id is None and hit.product_name is None


def test_suggested_name_matches_an_existing_product(client, db):
    melk = _product(db, "Halfvolle melk 1L")
    db.commit()
    hit = MatchIndex(db).find("lidl", "MELK HV", "halfvolle melk 1l")
    assert hit.source == "fuzzy" and hit.product_id == melk.id


def test_unmatched_line_proposes_a_new_product_from_the_suggestion(client, db):
    hit = MatchIndex(db).find("lidl", "KIPFILET 500G", "Kipfilet 500g")
    assert hit.source == "none" and hit.product_id is None and hit.product_name == "Kipfilet 500g"


def test_empty_index_and_empty_text_do_not_crash(client, db):
    hit = MatchIndex(db).find("plus", "", None)
    assert hit.source == "none"
