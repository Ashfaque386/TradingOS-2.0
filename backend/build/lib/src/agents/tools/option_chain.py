"""option-chain-read (Build Spec §16 starter skill set). Stub: no live
broker/options data feed is wired yet.
"""


def option_chain_read(params: dict) -> dict:
    return {
        "underlying": params.get("underlying"),
        "legs": [],
        "note": "stub: no live option chain feed wired yet",
    }
