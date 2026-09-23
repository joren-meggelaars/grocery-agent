from datetime import date

import pytest

from grocery.analytics.aggregate import (
    NOT_ITEMISED,
    CategoryMeta,
    LineFact,
    MonthPoint,
    ReceiptFact,
    add_months,
    against_reference,
    attribute,
    current_cycle_anchor,
    cycle_bounds,
    cycle_start,
    days_in_month,
    month_series,
    period_start,
    project_month_end,
    summarize_month,
    top_products,
    trend,
)

GROENTE, ZUIVEL, VLEES, HUIS, OVERIG = 1, 2, 3, 9, 10
CATS = {
    GROENTE: CategoryMeta(GROENTE, "Groente & fruit", True),
    ZUIVEL: CategoryMeta(ZUIVEL, "Zuivel & eieren", True),
    VLEES: CategoryMeta(VLEES, "Vlees & vis", True),
    HUIS: CategoryMeta(HUIS, "Huishouden & verzorging", False),
    OVERIG: CategoryMeta(OVERIG, "Overig", True),
}


def item(amount, cat=None, product=None, name=None, qty=1000, unit="pcs"):
    return LineFact("item", amount, cat, product, name, qty, unit)


def line(kind, amount):
    return LineFact(kind, amount, None)


def receipt(rid, on, store, total, *lines):
    return ReceiptFact(rid, on, store, total, tuple(lines))


SEP = date(2026, 9, 1)


# --- calendar helpers -------------------------------------------------------

def test_add_months_crosses_year_boundaries():
    assert add_months(date(2026, 1, 1), -1) == date(2025, 12, 1)
    assert add_months(date(2026, 11, 1), 3) == date(2027, 2, 1)
    assert add_months(date(2026, 9, 1), 0) == SEP


def test_days_in_month_including_leap_february():
    assert (days_in_month(date(2026, 2, 1)), days_in_month(date(2028, 2, 1)), days_in_month(SEP)) == (28, 29, 30)


# --- the spending cycle: payday to payday -----------------------------------

def test_a_cycle_starting_on_day_one_is_a_calendar_month():
    assert cycle_start(SEP, 1) == SEP
    assert cycle_bounds(SEP, 1) == (SEP, date(2026, 10, 1))


def test_a_cycle_can_start_on_any_other_day():
    assert cycle_start(SEP, 23) == date(2026, 9, 23)
    assert cycle_bounds(SEP, 23) == (date(2026, 9, 23), date(2026, 10, 23))


def test_a_late_start_day_is_clamped_to_the_days_february_has():
    assert cycle_start(date(2026, 2, 1), 31) == date(2026, 2, 28)
    assert cycle_start(date(2028, 2, 1), 31) == date(2028, 2, 29)  # leap year


def test_current_cycle_anchor_is_this_month_once_payday_has_passed():
    assert current_cycle_anchor(date(2026, 9, 23), 23) == SEP
    assert current_cycle_anchor(date(2026, 9, 30), 23) == SEP


def test_current_cycle_anchor_is_last_month_before_payday():
    assert current_cycle_anchor(date(2026, 9, 22), 23) == date(2026, 8, 1)
    assert current_cycle_anchor(date(2026, 9, 1), 23) == date(2026, 8, 1)


def test_current_cycle_anchor_with_a_calendar_month_is_todays_month():
    assert current_cycle_anchor(date(2026, 9, 1), 1) == SEP
    assert current_cycle_anchor(date(2026, 9, 30), 1) == SEP


# --- attribution ------------------------------------------------------------

def test_a_discount_is_booked_to_the_item_above_it():
    lines = [item(300, VLEES), line("discount", -100), item(200, ZUIVEL)]
    assert attribute(lines) == [(VLEES, 300), (VLEES, -100), (ZUIVEL, 200)]


def test_a_discount_with_no_item_before_it_and_other_charges_use_the_fallback():
    lines = [line("discount", -50), item(100, ZUIVEL), line("deposit", 25), line("bag", 10), line("rounding", -1)]
    assert attribute(lines) == [(None, -50), (ZUIVEL, 100), (None, 25), (None, 10), (None, -1)]


# --- month summary ----------------------------------------------------------

def test_totals_categories_and_stores_add_up():
    receipts = [
        receipt(1, date(2026, 9, 3), "Plus", 700, item(400, GROENTE), item(300, ZUIVEL)),
        receipt(2, date(2026, 9, 10), "Jumbo", 500, item(500, VLEES)),
    ]
    s = summarize_month(receipts, CATS, SEP)
    assert (s.receipts, s.total_cents, s.food_cents, s.nonfood_cents, s.not_itemised_cents) == (2, 1200, 1200, 0, 0)
    assert [(r.label, r.cents) for r in s.by_category] == [("Vlees & vis", 500), ("Groente & fruit", 400), ("Zuivel & eieren", 300)]
    assert [(r.label, r.cents) for r in s.by_store] == [("Plus", 700), ("Jumbo", 500)]
    assert sum(r.cents for r in s.by_category) == sum(r.cents for r in s.by_store) == s.total_cents


def test_household_spend_is_kept_out_of_the_food_figure_but_in_the_total():
    r = receipt(1, date(2026, 9, 3), "Plus", 1000, item(600, ZUIVEL), item(400, HUIS))
    s = summarize_month([r], CATS, SEP)
    assert (s.total_cents, s.food_cents, s.nonfood_cents) == (1000, 600, 400)
    assert {row.label: row.food for row in s.by_category} == {"Zuivel & eieren": True, "Huishouden & verzorging": False}


def test_discounts_reduce_their_category_and_food_plus_nonfood_is_the_total():
    r = receipt(1, date(2026, 9, 3), "Plus", 750, item(500, VLEES), line("discount", -150), item(400, HUIS))
    s = summarize_month([r], CATS, SEP)
    by = {row.label: row.cents for row in s.by_category}
    assert by == {"Vlees & vis": 350, "Huishouden & verzorging": 400}
    assert s.food_cents + s.nonfood_cents == s.total_cents == 750


def test_receipt_differences_show_up_as_not_itemised_and_count_as_food():
    r = receipt(1, date(2026, 9, 3), "Plus", 1000, item(900, ZUIVEL))  # saved with a 1.00 difference
    s = summarize_month([r], CATS, SEP)
    assert s.not_itemised_cents == 100 and s.food_cents == 1000 and s.total_cents == 1000
    assert (NOT_ITEMISED, 100) in [(row.label, row.cents) for row in s.by_category]


def test_lines_exceeding_the_total_give_a_negative_not_itemised_row():
    r = receipt(1, date(2026, 9, 3), "Plus", 900, item(1000, ZUIVEL))
    s = summarize_month([r], CATS, SEP)
    assert s.not_itemised_cents == -100 and s.food_cents == 900


def test_uncategorised_lines_and_other_charges_land_in_overig():
    r = receipt(1, date(2026, 9, 3), "Plus", 535, item(500, None), line("deposit", 25), line("bag", 10))
    s = summarize_month([r], CATS, SEP)
    assert [(row.label, row.cents) for row in s.by_category] == [("Overig", 535)]


def test_only_receipts_of_that_calendar_month_count():
    receipts = [
        receipt(1, date(2026, 8, 31), "Plus", 100, item(100, ZUIVEL)),
        receipt(2, date(2026, 9, 1), "Plus", 200, item(200, ZUIVEL)),
        receipt(3, date(2026, 9, 30), "Plus", 400, item(400, ZUIVEL)),
        receipt(4, date(2026, 10, 1), "Plus", 800, item(800, ZUIVEL)),
    ]
    s = summarize_month(receipts, CATS, date(2026, 9, 17))  # any day in the month selects it
    assert (s.month, s.receipts, s.total_cents) == (SEP, 2, 600)


def test_an_empty_month_is_all_zeros():
    s = summarize_month([], CATS, SEP)
    assert (s.receipts, s.total_cents, s.food_cents, s.by_category, s.by_store) == (0, 0, 0, [], [])


def test_shares_are_fractions_of_the_month_total():
    receipts = [receipt(1, date(2026, 9, 3), "Plus", 1000, item(750, ZUIVEL), item(250, VLEES))]
    s = summarize_month(receipts, CATS, SEP)
    assert [round(r.share, 2) for r in s.by_category] == [0.75, 0.25]
    assert s.by_store[0].share == pytest.approx(1.0)


def test_a_refund_only_month_does_not_divide_by_zero():
    s = summarize_month([receipt(1, date(2026, 9, 3), "Plus", 0, item(500, ZUIVEL), item(-500, ZUIVEL))], CATS, SEP)
    assert s.total_cents == 0 and s.by_category == []


# --- series and trend -------------------------------------------------------

def test_series_is_oldest_first_with_zero_months_and_crosses_the_year():
    receipts = [
        receipt(1, date(2025, 11, 5), "Plus", 300, item(300, ZUIVEL)),
        receipt(2, date(2026, 1, 5), "Plus", 500, item(500, ZUIVEL)),
    ]
    series = month_series(receipts, CATS, date(2026, 1, 20), months=4)
    assert [p.month for p in series] == [date(2025, 10, 1), date(2025, 11, 1), date(2025, 12, 1), date(2026, 1, 1)]
    assert [p.food_cents for p in series] == [0, 300, 0, 500]
    assert [p.receipts for p in series] == [0, 1, 0, 1]


def test_summarize_month_follows_a_payday_cycle():
    receipts = [
        receipt(1, date(2026, 9, 22), "Plus", 500, item(500, ZUIVEL)),  # the day before payday: previous cycle
        receipt(2, date(2026, 9, 23), "Plus", 700, item(700, ZUIVEL)),  # payday itself: this cycle
        receipt(3, date(2026, 10, 22), "Plus", 900, item(900, ZUIVEL)),  # the last day of this cycle
        receipt(4, date(2026, 10, 23), "Plus", 100, item(100, ZUIVEL)),  # next cycle
    ]
    s = summarize_month(receipts, CATS, SEP, start_day=23)
    assert s.month == date(2026, 9, 23) and s.total_cents == 700 + 900


def test_month_series_follows_a_payday_cycle():
    receipts = [receipt(1, date(2026, 9, 23), "Plus", 700, item(700, ZUIVEL))]
    series = month_series(receipts, CATS, SEP, months=2, start_day=23)
    assert [p.month for p in series] == [date(2026, 8, 23), date(2026, 9, 23)]
    assert [p.food_cents for p in series] == [0, 700]


def point(month, food, receipts=1):
    return MonthPoint(date(2026, month, 1), food, food, receipts)


def test_trend_compares_with_the_previous_month_and_the_recent_average():
    series = [point(5, 30000), point(6, 36000), point(7, 39000), point(8, 40000), point(9, 44000)]
    t = trend(series)
    assert (t.previous_food_cents, t.vs_previous_cents) == (40000, 4000)
    assert t.vs_previous_pct == pytest.approx(0.10)
    assert t.average_food_cents == 38333 and t.vs_average_cents == 44000 - 38333  # mean of Jun, Jul, Aug


def test_trend_ignores_months_without_receipts():
    series = [point(6, 0, receipts=0), point(7, 0, receipts=0), point(8, 40000), point(9, 20000)]
    t = trend(series)
    assert t.average_food_cents == 40000 and t.vs_previous_cents == -20000


def test_trend_without_history_says_nothing():
    t = trend([point(8, 0, receipts=0), point(9, 20000)])
    assert (t.previous_food_cents, t.vs_previous_cents, t.vs_previous_pct, t.average_food_cents) == (None, None, None, None)
    only = trend([point(9, 20000)])
    assert only.previous_food_cents is None


# --- reference and pace -----------------------------------------------------

@pytest.mark.parametrize(
    "spent,state",
    [(0, "ok"), (31999, "ok"), (32000, "warn"), (39999, "warn"), (40000, "warn"), (40001, "over"), (60000, "over")],
)
def test_reference_states(spent, state):
    ref = against_reference(spent, 40000)
    assert ref.state == state and ref.remaining_cents == 40000 - spent
    assert ref.used_pct == pytest.approx(spent / 40000)


def test_a_zero_reference_does_not_crash():
    assert against_reference(500, 0).used_pct == 0.0


def test_projection_extrapolates_the_running_month_only():
    assert project_month_end(20000, SEP, date(2026, 9, 15)) == 40000  # 200 in 15 days -> 400 in 30
    assert project_month_end(20000, SEP, date(2026, 9, 6)) is None  # too early to say
    assert project_month_end(20000, date(2026, 8, 1), date(2026, 9, 15)) is None  # a past month
    assert project_month_end(20000, date(2026, 10, 1), date(2026, 9, 15)) is None  # a future month


def test_projection_follows_a_payday_cycle_instead_of_the_calendar_month():
    # cycle: 23 Sep - 22 Oct (30 days). 8 days in (30 Sep), elapsed=8, spent 16000 -> pace 60000.
    assert project_month_end(16000, SEP, date(2026, 9, 30), start_day=23) == 60000
    assert project_month_end(16000, SEP, date(2026, 9, 20), start_day=23) is None  # before this cycle starts
    assert project_month_end(16000, SEP, date(2026, 9, 27), start_day=23) is None  # only 5 days in: too early


# --- regulars ---------------------------------------------------------------

def basket():
    return [
        receipt(1, date(2026, 9, 1), "Plus", 0,
                item(258, ZUIVEL, 1, "Halfvolle melk 1L", qty=2000), item(742, VLEES, 2, "Kipfilet", qty=874, unit="kg"),
                item(100, None)),
        receipt(2, date(2026, 9, 8), "Jumbo", 0,
                item(129, ZUIVEL, 1, "Halfvolle melk 1L"), item(129, ZUIVEL, 1, "Halfvolle melk 1L")),
        receipt(3, date(2026, 9, 15), "Turkish supermarket", 0,
                item(849, VLEES, 2, "Kipfilet", qty=1000, unit="kg"), line("discount", -50)),
        receipt(4, date(2026, 6, 1), "Plus", 0, item(300, GROENTE, 3, "Bananen")),
    ]


def test_regulars_count_receipts_not_lines():
    stats, _ = top_products(basket(), since=None)
    melk = next(s for s in stats if s.name == "Halfvolle melk 1L")
    assert melk.times_bought == 2  # two lines on receipt 2 still count as one visit
    assert melk.total_cents == 258 + 129 + 129


def test_regulars_are_sorted_by_visits_then_spend():
    stats, _ = top_products(basket(), since=None)
    assert [(s.name, s.times_bought) for s in stats] == [("Kipfilet", 2), ("Halfvolle melk 1L", 2), ("Bananen", 1)]
    # Kipfilet (742 + 849) beats melk (516) on the tie


def test_typical_price_is_per_pack_or_per_kg():
    stats, _ = top_products(basket(), since=None)
    melk = next(s for s in stats if s.name == "Halfvolle melk 1L")
    kip = next(s for s in stats if s.name == "Kipfilet")
    assert (melk.basis, melk.typical_price_cents) == ("pack", 129)  # 2 x 1.29, 1.29, 1.29
    assert kip.basis == "kg" and kip.typical_price_cents == round((742 * 1000 / 874 + 849) / 2)


def test_regulars_remember_the_latest_purchase():
    stats, _ = top_products(basket(), since=None)
    kip = next(s for s in stats if s.name == "Kipfilet")
    assert (kip.last_on, kip.last_store) == (date(2026, 9, 15), "Turkish supermarket")


def test_lines_without_a_product_are_counted_not_listed():
    stats, unmatched = top_products(basket(), since=None)
    assert unmatched == 1 and all(s.name for s in stats)


def test_the_period_filter_and_limit():
    stats, _ = top_products(basket(), since=date(2026, 9, 1))
    assert "Bananen" not in [s.name for s in stats]
    assert len(top_products(basket(), since=None, limit=1)[0]) == 1


def test_period_start():
    today = date(2026, 9, 20)
    assert period_start("month", today) == SEP
    assert period_start("3m", today) == date(2026, 6, 22)
    assert period_start("12m", today) == date(2025, 9, 20)
    assert period_start("all", today) is None
    assert period_start("nonsense", today) is None


def test_period_start_month_follows_the_payday_cycle():
    assert period_start("month", date(2026, 9, 20), start_day=23) == date(2026, 8, 23)  # payday not reached yet
    assert period_start("month", date(2026, 9, 23), start_day=23) == date(2026, 9, 23)
