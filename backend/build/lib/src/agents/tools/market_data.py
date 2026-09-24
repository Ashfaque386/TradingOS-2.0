"""market-data-read (Build Spec §16 starter skill set). Stub: no live
market data feed is wired yet (data-ingestion phase); returns a
structurally-shaped, honestly-empty result rather than fabricating a price.
"""


def market_data_read(params: dict) -> dict:
    return {
        "symbol": params.get("symbol"),
        "price": None,
        "note": "stub: no live market data feed wired yet",
    }
