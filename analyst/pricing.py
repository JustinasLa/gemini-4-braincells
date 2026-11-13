"""Gemini API list prices (USD per 1M tokens, paid tier, Standard), for cost estimates in logs,
verdicts and the model comparison. Thinking tokens are billed as output.

Source: https://ai.google.dev/gemini-api/docs/pricing (redirects to
aistudio.google.com/docs/pricing), read 2026-11-13. Introductory prices are noted where the
page gives an end date.
"""

PRICES: dict[str, tuple[float, float]] = {
    "gemini-4-hydrogen": (1.50, 7.50),
    "gemini-4-argon": (2.00, 10.00),  # until 2026-11-30, then 4.00 / 20.00
    "gemini-4-carbon": (2.00, 10.00),  # until 2026-11-30, then 4.00 / 20.00
    "gemini-3.8-flash": (0.75, 3.75),  # until 2026-12-31, then 1.50 / 7.50
    "gemini-3.5-flash-lite": (0.30, 2.50),
}


def cost_usd(model: str, prompt_tokens: int | None, output_tokens: int | None) -> float | None:
    """Estimated cost of one call; None for a model without a listed price."""
    price = PRICES.get(model.removeprefix("models/"))
    if price is None:
        return None
    return ((prompt_tokens or 0) * price[0] + (output_tokens or 0) * price[1]) / 1_000_000
