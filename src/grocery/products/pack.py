"""Which products still need their pack content asked, so that they get a price per kg or l."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from grocery.db.models import Category, Product
from grocery.products.content import format_content, is_sold_per_piece
from grocery.products.normalize import product_key


def pack_questions(db: Session, items: list[tuple[str, int | None]]) -> list[dict | None]:
    """For each (product name, category id): None when nothing needs asking, else
    {"known": pack content is stored on the product, "content": text to prefill}."""
    keys = {product_key(name) for name, _ in items if name.strip()}
    products = {
        p.name_key: p for p in db.scalars(select(Product).where(Product.name_key.in_(keys)))
    } if keys else {}
    categories = {c.id: c.name for c in db.scalars(select(Category))}
    result = []
    for name, category_id in items:
        if not name.strip():
            result.append(None)
            continue
        product = products.get(product_key(name))
        if product is not None and product.pack_content and product.pack_basis and not product.sold_per_piece:
            result.append({"known": True, "content": format_content(product.pack_content, product.pack_basis)})
            continue
        category = categories.get(category_id) or (categories.get(product.category_id) if product else None)
        if is_sold_per_piece(name, category, bool(product and product.sold_per_piece)):
            result.append(None)
        else:
            result.append({"known": False, "content": ""})
    return result
