"""Chart geometry for the overview. The pages draw plain inline SVG from these numbers, so there is no
charting library, no script needed to draw, and nothing that the CSP has to allow.

Design (see the dataviz guidance): one series per chart, so no categorical colours and no legend box;
the current month is the accent and earlier months recede in grey (emphasis); bars are at most 24px
thick with a 4px rounded data end and a square baseline; one hairline axis; the value sits at the end
of the bar; every chart has a table next to it with all the numbers.
"""

import math
from dataclasses import dataclass, field

from grocery.analytics.aggregate import MonthPoint, Row

MAX_BAR = 24  # px, never fill the band
BAR_RADIUS = 4
NAME_LIMIT = 24  # characters of a category or store name that fit next to a bar


def eur(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}€{abs(cents) // 100:,}.{abs(cents) % 100:02d}"


def eur_compact(cents: int) -> str:
    """Axis labels: €400, €1,200 (no cents)."""
    return f"€{round(cents / 100):,}"


def nice_max(value: float) -> float:
    """Round an axis maximum up to 1, 2, 2.5, 5 or 10 times a power of ten."""
    if value <= 0:
        return 1.0
    base = 10 ** math.floor(math.log10(value))
    for m in (1, 2, 2.5, 5, 10):
        if value <= m * base:
            return m * base
    return 10 * base


def rounded_top(x: float, y: float, w: float, h: float, r: float = BAR_RADIUS) -> str:
    """A column with rounded top corners and a square baseline; empty for a zero-height bar."""
    if h <= 0 or w <= 0:
        return ""
    r = min(r, h, w / 2)
    b = y + h
    return (
        f"M{x:.1f},{b:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
        f"H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{b:.1f} Z"
    )


def rounded_right(x: float, y: float, w: float, h: float, r: float = BAR_RADIUS) -> str:
    """A horizontal bar with a rounded right end and a square left edge on the baseline."""
    if h <= 0 or w <= 0:
        return ""
    r = min(r, w, h / 2)
    return (
        f"M{x:.1f},{y:.1f} H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} "
        f"V{y + h - r:.1f} Q{x + w:.1f},{y + h:.1f} {x + w - r:.1f},{y + h:.1f} H{x:.1f} Z"
    )


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


@dataclass
class Mark:
    path: str  # empty for a zero value
    hit: tuple[float, float, float, float]  # x, y, w, h of the tap/hover target, larger than the bar
    tip: str
    highlight: bool = False
    label: str = ""
    label_xy: tuple[float, float] = (0.0, 0.0)  # value label position
    name: str = ""
    name_xy: tuple[float, float] = (0.0, 0.0)  # month / category name position
    title: str = ""  # full text when the name was shortened


@dataclass
class Tick:
    y: float
    text: str


@dataclass
class ColumnChart:
    width: int
    height: int
    aria: str
    marks: list[Mark] = field(default_factory=list)
    ticks: list[Tick] = field(default_factory=list)
    baseline_y: float = 0.0
    plot_left: float = 0.0
    plot_right: float = 0.0
    ref_y: float | None = None
    ref_label: str = ""
    ref_name: str = ""
    ref_value: str = ""


def column_chart(points: list[MonthPoint], reference_cents: int, ref_name: str = "Reference") -> ColumnChart:
    width, height = 340, 210
    left, right, top, bottom = 46, 62, 22, 26  # the right margin holds the reference label
    plot_w, plot_h = width - left - right, height - top - bottom
    baseline = top + plot_h
    values = [p.food_cents for p in points]
    ceiling = nice_max(max([max(values, default=0), reference_cents]) * 1.1 / 100)  # euros
    scale = lambda cents: plot_h * (cents / 100) / ceiling  # noqa: E731

    chart = ColumnChart(width, height, "", baseline_y=baseline, plot_left=left, plot_right=width - right)
    chart.aria = "Food spend per month: " + ", ".join(f"{p.month:%b %Y} {eur(p.food_cents)}" for p in points)
    for value in (0, ceiling / 2, ceiling):
        chart.ticks.append(Tick(baseline - plot_h * value / ceiling, eur_compact(round(value * 100))))
    chart.ref_y = baseline - scale(reference_cents)
    chart.ref_name, chart.ref_value = ref_name, eur_compact(reference_cents)
    chart.ref_label = f"{ref_name} {chart.ref_value}"

    band = plot_w / max(len(points), 1)
    bar_w = min(MAX_BAR, band * 0.5)
    for i, p in enumerate(points):
        x = left + band * i + (band - bar_w) / 2
        h = max(0.0, scale(p.food_cents))
        current = i == len(points) - 1
        chart.marks.append(
            Mark(
                path=rounded_top(x, baseline - h, bar_w, h),
                hit=(left + band * i, top, band, plot_h + bottom),
                tip=f"{p.month:%B %Y}: {eur(p.food_cents)} food"
                + (f", {eur(p.total_cents)} in total" if p.total_cents != p.food_cents else ""),
                highlight=current,
                label=eur(p.food_cents) if current and p.receipts else "",
                label_xy=(x + bar_w / 2, baseline - h - 6),
                name=f"{p.month:%b}",
                name_xy=(x + bar_w / 2, height - 8),
            )
        )
    return chart


@dataclass
class BarChart:
    width: int
    height: int
    aria: str
    marks: list[Mark] = field(default_factory=list)
    baseline_x: float = 0.0


def bar_chart(rows: list[Row], aria: str, max_rows: int = 8) -> BarChart:
    """Horizontal bars, largest first. Rows beyond max_rows are left to the table."""
    shown = [r for r in rows if r.cents > 0][:max_rows]
    width, row_h, thickness = 340, 30, 16
    label_w, value_w, pad = 130, 74, 6
    bar_max = width - label_w - value_w - pad
    height = max(row_h * len(shown), row_h) + 4
    chart = BarChart(width, height, aria, baseline_x=label_w)
    biggest = max((r.cents for r in shown), default=1)
    for i, r in enumerate(shown):
        y = 2 + i * row_h
        length = bar_max * r.cents / biggest
        by = y + (row_h - thickness) / 2
        chart.marks.append(
            Mark(
                path=rounded_right(label_w, by, length, thickness),
                hit=(0, y, width, row_h),
                tip=f"{r.label}: {eur(r.cents)} ({r.share:.0%})",
                highlight=True,
                label=eur(r.cents),
                label_xy=(label_w + length + pad, y + row_h / 2 + 4),
                name=truncate(r.label, NAME_LIMIT),
                name_xy=(label_w - pad, y + row_h / 2 + 4),
                title=r.label if len(r.label) > NAME_LIMIT else "",
            )
        )
    return chart
