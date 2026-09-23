"""A small, curated set of non-secret settings that can be changed from the Settings page, without
touching .env or redeploying. Stored as rows in `settings` (key/value text), prefixed "app.", which
override the process defaults from .env for the lifetime of the row.

What is deliberately NOT here: the Claude API key and anything else secret; network/security
settings (allowed hosts, Tailscale, cookies, sessions) that would need care and a restart to change
safely; and the reading model/effort, which is baked into the background worker's reader objects
when a job starts rather than read fresh per request.
"""

from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from grocery.config import Settings
from grocery.db.models import Setting
from grocery.deals.client import CATEGORY_SLUGS

PREFIX = "app."
EFFORT = ("low", "medium", "high")
ONOFF = ("on", "off")


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    help: str
    kind: str  # "int" | "float" | "str" | "choice"
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    optional: bool = False  # an empty value is allowed


FIELDS = [
    Field(
        "cycle_start_day", "Spending cycle starts on day",
        "Day of the month your spending month begins, e.g. payday. 1 is a calendar month.",
        "int", minimum=1, maximum=28,
    ),
    Field(
        "monthly_reference_eur", "Monthly food reference (EUR)",
        "What food should cost per cycle; the overview compares spend against this.",
        "float", minimum=1, maximum=100000,
    ),
    Field("timezone", "Timezone", "IANA name, e.g. Europe/Amsterdam. Used for \"today\" and displayed times.", "str"),
    Field(
        "llm_monthly_budget_eur", "Claude API monthly budget (EUR)",
        "Hard stop for new extractions once this month's estimated cost reaches it.",
        "float", minimum=0, maximum=1000,
    ),
    Field("deals_enabled", "Deals radar", "Fetch the weekly offers of Jumbo, Plus, Aldi and Lidl every night.", "choice",
          choices=ONOFF),
    Field("deals_alerts", "Deal alerts", "Send a Home Assistant notification for new deals worth a look.", "choice",
          choices=ONOFF),
    Field(
        "deals_mine_min_pct", "Your products: cheaper than usual by (%)",
        "Alert for a product you buy when the offer is at least this much below what you usually pay.",
        "int", minimum=1, maximum=90,
    ),
    Field(
        "deals_notable_min_pct", "Rarely bought: discount of at least (%)",
        "For the categories below: alert on offers with at least this discount, even for products you never bought.",
        "int", minimum=10, maximum=90,
    ),
    Field(
        "deals_notable_min_eur", "Rarely bought: saving of at least (EUR)",
        "...and at least this much saved per item.", "float", minimum=0, maximum=100,
    ),
    Field(
        "deals_categories", "Rarely bought: categories to watch",
        "Comma separated: " + ", ".join(CATEGORY_SLUGS) + ". Empty switches this kind of alert off.",
        "str", optional=True,
    ),
    Field(
        "confirmed_retention_days", "Keep photos after saving (days)",
        "A receipt or label photo is deleted this many days after you confirm it.",
        "int", minimum=1, maximum=365,
    ),
    Field(
        "unconfirmed_retention_days", "Keep photos never confirmed (days)",
        "A photo you never review or confirm is deleted after this many days.",
        "int", minimum=1, maximum=365,
    ),
]
BY_KEY = {f.key: f for f in FIELDS}


@dataclass(frozen=True)
class Effective:
    cycle_start_day: int
    monthly_reference_eur: float
    timezone: str
    llm_monthly_budget_eur: float
    confirmed_retention_days: int
    unconfirmed_retention_days: int
    deals_enabled: str
    deals_alerts: str
    deals_mine_min_pct: int
    deals_notable_min_pct: int
    deals_notable_min_eur: float
    deals_categories: str


class SettingsError(ValueError):
    def __init__(self, messages: list[str]) -> None:
        super().__init__("; ".join(messages))
        self.messages = messages


def _defaults(settings: Settings) -> dict[str, object]:
    return {
        "cycle_start_day": 1,  # a calendar month, until the Settings page says otherwise
        "monthly_reference_eur": settings.monthly_reference_eur,
        "timezone": settings.timezone,
        "llm_monthly_budget_eur": settings.llm_monthly_budget_eur,
        "confirmed_retention_days": settings.confirmed_retention_days,
        "unconfirmed_retention_days": settings.unconfirmed_retention_days,
        "deals_enabled": "on",
        "deals_alerts": "on",
        "deals_mine_min_pct": 10,
        "deals_notable_min_pct": 30,
        "deals_notable_min_eur": 2.0,
        "deals_categories": "huishouden,drogisterij",
    }


def _cast(field: Field, raw: str):
    if field.kind == "int":
        return int(raw)
    if field.kind == "float":
        return float(raw)
    return raw


def effective(db: Session, settings: Settings) -> Effective:
    values = _defaults(settings)
    rows = db.scalars(select(Setting).where(Setting.key.like(f"{PREFIX}%")))
    for row in rows:
        field = BY_KEY.get(row.key[len(PREFIX):])
        if field is None:
            continue  # a row this version of the app does not know: ignore rather than crash
        try:
            value = _cast(field, row.value)
        except ValueError:
            continue  # a corrupted row must never break the app; fall back to the default
        if field.kind == "choice" and value not in field.choices:
            continue
        values[field.key] = value
    return Effective(**values)


def _valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return False
    return True


def parse_and_save(db: Session, settings: Settings, form) -> Effective:
    errors: list[str] = []
    to_save: dict[str, str] = {}
    for field in FIELDS:
        raw = (form.get(field.key) or "").strip()
        if not raw and not field.optional:
            errors.append(f"{field.label}: enter a value.")
            continue
        if field.kind == "choice":
            if raw not in field.choices:
                errors.append(f"{field.label}: choose {' or '.join(field.choices)}.")
                continue
            to_save[field.key] = raw
        elif field.kind == "str":
            if field.key == "timezone" and not _valid_timezone(raw):
                errors.append(f"{field.label}: not a known timezone name.")
                continue
            if field.key == "deals_categories":
                slugs = [p.strip().lower() for p in raw.split(",") if p.strip()]
                unknown = [p for p in slugs if p not in CATEGORY_SLUGS]
                if unknown:
                    errors.append(f"{field.label}: unknown category {', '.join(unknown)}.")
                    continue
                raw = ",".join(dict.fromkeys(slugs))
            to_save[field.key] = raw
        else:
            try:
                value = _cast(field, raw.replace(",", "."))
            except ValueError:
                errors.append(f"{field.label}: not a valid number.")
                continue
            if (field.minimum is not None and value < field.minimum) or (
                field.maximum is not None and value > field.maximum
            ):
                errors.append(f"{field.label}: must be between {field.minimum:g} and {field.maximum:g}.")
                continue
            to_save[field.key] = str(value)  # the canonical form: effective() must be able to re-parse it
    if errors:
        raise SettingsError(errors)

    for key, raw in to_save.items():
        row = db.get(Setting, f"{PREFIX}{key}")
        if row is None:
            db.add(Setting(key=f"{PREFIX}{key}", value=raw))
        else:
            row.value = raw
    db.commit()
    return effective(db, settings)
