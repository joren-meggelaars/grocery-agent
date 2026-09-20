from datetime import date

import pytest

from grocery.analytics.aggregate import MonthPoint, Row
from grocery.analytics.charts import (
    MAX_BAR,
    bar_chart,
    column_chart,
    eur,
    eur_compact,
    nice_max,
    rounded_right,
    rounded_top,
    truncate,
)


def months(*foods):
    return [
        MonthPoint(date(2026, 4 + i, 1), food, food, 1 if food else 0) for i, food in enumerate(foods)
    ]


# --- formatting -------------------------------------------------------------

def test_euro_formatting():
    assert eur(123456) == "€1,234.56" and eur(5) == "€0.05" and eur(-50) == "-€0.50" and eur(0) == "€0.00"
    assert eur_compact(40000) == "€400" and eur_compact(120000) == "€1,200"


@pytest.mark.parametrize(
    "value,expected",
    [(0, 1), (-5, 1), (0.9, 1), (1.2, 2), (2.3, 2.5), (3.7, 5), (5.1, 10), (410, 500), (501, 1000), (1234, 2000)],
)
def test_nice_axis_maximum(value, expected):
    assert nice_max(value) == expected


def test_truncate():
    assert truncate("short", 17) == "short"
    assert truncate("Huishouden & verzorging", 17) == "Huishouden & ver…" and len(truncate("x" * 40, 17)) == 17


# --- mark shapes ------------------------------------------------------------

def test_a_column_has_rounded_top_corners_and_a_square_baseline():
    path = rounded_top(10, 20, 24, 60)
    assert path.startswith("M10.0,80.0")  # starts on the baseline (y + h), square corner
    assert path.count("Q") == 2  # two rounded corners, both at the top


def test_the_corner_radius_never_exceeds_the_bar():
    assert "Q" in rounded_top(0, 0, 24, 2)  # a tiny bar still renders, radius clamped to its height
    assert rounded_top(0, 0, 24, 0) == "" and rounded_top(0, 0, 0, 10) == ""


def test_a_horizontal_bar_is_rounded_on_the_right_only():
    path = rounded_right(100, 10, 80, 16)
    assert path.startswith("M100.0,10.0") and path.count("Q") == 2 and rounded_right(0, 0, 0, 16) == ""


# --- column chart -----------------------------------------------------------

def test_columns_are_proportional_capped_and_anchored_to_the_baseline():
    chart = column_chart(months(20000, 40000, 0, 30000, 10000, 44000), reference_cents=40000)
    assert len(chart.marks) == 6 and chart.baseline_y > 0
    assert chart.marks[2].path == ""  # an empty month draws no bar but keeps its hit area
    heights = {}
    for m in chart.marks:
        if m.path:
            ys = [float(p.split(",")[1].split()[0]) for p in m.path.replace("M", "").replace("V", " ").split() if "," in p]
            heights[m.name] = chart.baseline_y - min(ys)
    assert heights["May"] == pytest.approx(2 * heights["Apr"], rel=0.03)  # 40000 is twice 20000
    assert all(m.hit[2] > MAX_BAR for m in chart.marks)  # tap target is wider than the bar


def test_bar_width_is_capped():
    chart = column_chart(months(10000, 20000), reference_cents=40000)  # two wide bands
    xs = [float(m.path.split(",")[0][1:]) for m in chart.marks]
    right_edges = [float(m.path.split("H")[1].split()[0]) for m in chart.marks]
    assert all(r - x <= MAX_BAR + 8 for x, r in zip(xs, right_edges))  # includes the 4px corner radius each side


def test_only_the_current_month_is_highlighted_and_labelled():
    chart = column_chart(months(20000, 40000, 44000), reference_cents=40000)
    assert [m.highlight for m in chart.marks] == [False, False, True]
    assert [bool(m.label) for m in chart.marks] == [False, False, True] and chart.marks[2].label == "€440.00"


def test_the_reference_line_is_inside_the_axis_and_sits_between_the_right_bars():
    chart = column_chart(months(20000, 60000), reference_cents=40000)
    assert chart.ref_label == "Reference €400" and (chart.ref_name, chart.ref_value) == ("Reference", "€400")
    top_tick = min(t.y for t in chart.ticks)
    assert top_tick <= chart.ref_y < chart.baseline_y  # a reference above every bar still fits on the axis
    high = column_chart(months(1000, 2000), reference_cents=40000)
    assert high.ref_y < high.baseline_y and high.ticks[-1].text == "€500"


def test_axis_ticks_and_accessible_summary():
    chart = column_chart(months(20000, 44000), reference_cents=40000)
    assert [t.text for t in chart.ticks] == ["€0", "€250", "€500"]
    assert "Apr 2026 €200.00" in chart.aria and "May 2026 €440.00" in chart.aria


def test_tooltip_mentions_non_food_when_it_differs():
    # MonthPoint(month, total_cents, food_cents, receipts)
    points = [MonthPoint(date(2026, 4, 1), 25000, 20000, 1), MonthPoint(date(2026, 5, 1), 30000, 30000, 1)]
    tips = [m.tip for m in column_chart(points, 40000).marks]
    assert "€200.00 food" in tips[0] and "€250.00 in total" in tips[0] and "in total" not in tips[1]


def test_all_zero_months_still_render():
    chart = column_chart(months(0, 0, 0), reference_cents=40000)
    assert all(m.path == "" for m in chart.marks) and chart.ref_y < chart.baseline_y


# --- bar chart --------------------------------------------------------------

def test_bars_are_relative_to_the_largest_and_sorted_as_given():
    rows = [Row("Vlees & vis", 50000, 0.5), Row("Zuivel & eieren", 25000, 0.25)]
    chart = bar_chart(rows, "Spend per category")
    long_end = float(chart.marks[0].path.split("H")[1].split()[0])
    short_end = float(chart.marks[1].path.split("H")[1].split()[0])
    left = chart.baseline_x
    assert (short_end - left + 4) == pytest.approx((long_end - left + 4) / 2, rel=0.05)
    assert chart.marks[0].label == "€500.00" and "(50%)" in chart.marks[0].tip


def test_value_label_sits_after_the_bar_and_inside_the_svg():
    chart = bar_chart([Row("A", 100, 1.0)], "x")
    assert chart.marks[0].label_xy[0] > chart.baseline_x
    assert chart.marks[0].label_xy[0] < chart.width - 40  # room for the text at the widest bar


def test_long_names_are_shortened_but_keep_the_full_text():
    long_name = "Een categorienaam die veel te lang is voor de zijkant"
    chart = bar_chart([Row(long_name, 5000, 1.0)], "x")
    assert chart.marks[0].name.endswith("…") and chart.marks[0].title == long_name
    assert bar_chart([Row("Huishouden & verzorging", 5000, 1.0)], "x").marks[0].title == ""  # this one fits
    assert bar_chart([Row("Zuivel", 5000, 1.0)], "x").marks[0].title == ""


def test_zero_and_negative_rows_are_not_drawn_and_the_list_is_capped():
    rows = [Row("A", 300, 0.5), Row("B", 0, 0.0), Row("C", -50, 0.0)] + [Row(f"R{i}", 10 + i, 0.01) for i in range(12)]
    chart = bar_chart(rows, "x", max_rows=8)
    assert len(chart.marks) == 8 and all(m.path for m in chart.marks)


def test_an_empty_bar_chart_is_valid():
    chart = bar_chart([], "x")
    assert chart.marks == [] and chart.height > 0
