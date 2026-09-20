"""Reference data that migration 0002 seeds. Tests seed the same rows via seed()."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from grocery.db.models import Category, Setting, Store

CATEGORIES = [
    ("Groente & fruit", True),
    ("Zuivel & eieren", True),
    ("Vlees & vis", True),
    ("Brood & banket", True),
    ("Dranken", True),
    ("Snacks & zoet", True),
    ("Houdbaar", True),
    ("Diepvries", True),
    ("Huishouden & verzorging", False),
    ("Overig", True),
]
FALLBACK_CATEGORY = "Overig"

STORES = [
    ("plus", "Plus", "regular"),
    ("jumbo", "Jumbo", "regular"),
    ("lidl", "Lidl", "target"),
    ("aldi", "Aldi", "other"),
    ("ah", "Albert Heijn", "other"),
    ("bakery", "Bakery", "regular"),
    ("turkish", "Turkish supermarket", "baseline"),
    ("other", "Other", "other"),
]

# Default category for manual quick-add, per store.
QUICKADD_CATEGORY = {"bakery": "Brood & banket", "turkish": "Vlees & vis"}
QUICKADD_NAME = {"bakery": "Brood", "turkish": "Kip"}
TURKISH_PRICE_KEY = "quickadd.turkish.price_per_kg_cents"


def seed(db: Session) -> None:
    if db.scalar(select(Store.id).limit(1)) is None:
        db.add_all(Store(chain=c, name=n, role=r) for c, n, r in STORES)
    if db.scalar(select(Category.id).limit(1)) is None:
        db.add_all(
            Category(name=n, counts_as_food=f, sort_order=i)
            for i, (n, f) in enumerate(CATEGORIES, start=1)
        )
    if db.get(Setting, TURKISH_PRICE_KEY) is None:
        db.add(Setting(key=TURKISH_PRICE_KEY, value="849"))
    db.commit()
