"""Cost estimates from logged token usage.

Prices are USD per million tokens as cached in the claude-api reference on 2026-06-24 and can be
out of date; they only drive the monthly budget guard and the numbers shown in the UI.
"""

import logging

log = logging.getLogger(__name__)

# model -> (input, output) USD per million tokens
PRICES_USD = {
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-opus-5": (5.0, 25.0),
}
CACHE_READ_FACTOR = 0.1
CACHE_WRITE_FACTOR = 1.25


def estimate_cost_eur(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    usd_to_eur: float = 0.92,
) -> float:
    prices = PRICES_USD.get(model)
    if prices is None:
        # Unknown model: assume the most expensive known price so the budget guard errs safe.
        log.warning("no price known for model %r; using the highest known price", model)
        prices = max(PRICES_USD.values())
    in_price, out_price = prices
    usd = (
        input_tokens * in_price
        + cache_read_tokens * in_price * CACHE_READ_FACTOR
        + cache_write_tokens * in_price * CACHE_WRITE_FACTOR
        + output_tokens * out_price
    ) / 1_000_000
    return round(usd * usd_to_eur, 6)
