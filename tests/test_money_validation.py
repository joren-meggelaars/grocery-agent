from datetime import date

import pytest

from grocery.money import ParseError, format_cents, format_quantity, parse_euro, parse_quantity_milli
from grocery.receipts.validation import LineData, expected_line_total, validate_receipt

TODAY = date(2026, 9, 20)


def L(no, kind, total, qty=1000, unit_price=None):
    return LineData(no, kind, qty, unit_price, total)


# --- money ------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,cents",
    [
        ("1,29", 129), ("1.29", 129), ("€ 1,29", 129), ("12", 1200), ("0,5", 50),
        ("-0,50", -50), ("1.234,56", 123456), ("1,234.56", 123456), ("  7,00 ", 700), ("0", 0),
    ],
)
def test_parse_euro(text, cents):
    assert parse_euro(text) == cents


@pytest.mark.parametrize("bad", ["", "abc", "1,2,3", "1.234", "--1", "1,299"])
def test_parse_euro_rejects_garbage(bad):
    with pytest.raises(ParseError):
        parse_euro(bad)


def test_format_cents_round_trips():
    for cents in (0, 5, 129, -50, 123456):
        assert parse_euro(format_cents(cents)) == cents
    assert format_cents(None) == ""


def test_quantities():
    assert parse_quantity_milli("2") == 2000
    assert parse_quantity_milli("0,874") == 874
    assert format_quantity(2000) == "2"
    assert format_quantity(874) == "0.874"
    with pytest.raises(ParseError):
        parse_quantity_milli("x")


# --- validation: totals -----------------------------------------------------

def test_simple_receipt_adds_up():
    lines = [L(1, "item", 129), L(2, "item", 249), L(3, "item", 89)]
    r = validate_receipt(lines, 467, date(2026, 9, 19), TODAY)
    assert r.delta_cents == 0 and r.ok


def test_discounts_reduce_the_sum():
    lines = [L(1, "item", 299), L(2, "discount", -100), L(3, "item", 149)]
    assert validate_receipt(lines, 348, date(2026, 9, 19), TODAY).ok


def test_deposit_bag_and_rounding_lines_count_towards_the_total():
    lines = [
        L(1, "item", 1000), L(2, "deposit", 25), L(3, "bag", 10),
        L(4, "deposit", -25), L(5, "rounding", -1),
    ]
    assert validate_receipt(lines, 1009, date(2026, 9, 19), TODAY).delta_cents == 0


def test_mismatch_reports_the_delta_and_the_flag():
    lines = [L(1, "item", 129), L(2, "item", 249)]
    r = validate_receipt(lines, 400, date(2026, 9, 19), TODAY)
    assert r.delta_cents == 22  # the receipt says more than the lines: a missing line
    assert "sum_mismatch" in r.flags and not r.ok


def test_negative_delta_means_lines_exceed_the_total():
    r = validate_receipt([L(1, "item", 500), L(2, "item", 500)], 900, date(2026, 9, 19), TODAY)
    assert r.delta_cents == -100


def test_missing_total_and_missing_lines_are_flagged():
    r = validate_receipt([], None, date(2026, 9, 19), TODAY)
    assert {"no_lines", "no_total"} <= set(r.flags)
    assert r.delta_cents is None


# --- validation: line math --------------------------------------------------

def test_expected_line_total_rounds_half_up():
    assert expected_line_total(2000, 129) == 258
    assert expected_line_total(874, 849) == 742  # 0.874 kg at 8.49/kg = 7.42
    assert expected_line_total(500, 101) == 51  # 50.5 rounds up


def test_weighed_item_within_one_cent_is_fine():
    lines = [L(1, "item", 742, qty=874, unit_price=849)]
    assert validate_receipt(lines, 742, date(2026, 9, 19), TODAY).ok
    off_by_one = [L(1, "item", 743, qty=874, unit_price=849)]
    assert validate_receipt(off_by_one, 743, date(2026, 9, 19), TODAY).ok


def test_line_math_error_is_flagged_on_that_line():
    lines = [L(1, "item", 300, qty=2000, unit_price=129), L(2, "item", 100)]
    r = validate_receipt(lines, 400, date(2026, 9, 19), TODAY)
    assert r.line_flags == {1: ["line_math"]}
    assert r.delta_cents == 0  # the total still adds up; only the line's own math is off


def test_line_math_is_skipped_without_a_unit_price():
    assert validate_receipt([L(1, "item", 300, qty=2000)], 300, date(2026, 9, 19), TODAY).ok


def test_sign_sanity_checks():
    lines = [L(1, "item", -100), L(2, "discount", 50)]
    r = validate_receipt(lines, -50, date(2026, 9, 19), TODAY)
    assert r.line_flags == {1: ["negative_item"], 2: ["positive_discount"]}


# --- validation: dates ------------------------------------------------------

def test_date_checks():
    lines = [L(1, "item", 100)]
    assert "no_date" in validate_receipt(lines, 100, None, TODAY).flags
    assert "date_future" in validate_receipt(lines, 100, date(2026, 9, 21), TODAY).flags
    assert "date_old" in validate_receipt(lines, 100, date(2025, 1, 1), TODAY).flags
    assert validate_receipt(lines, 100, TODAY, TODAY).ok
