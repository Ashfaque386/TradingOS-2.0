"""option-chain-read (Build Spec §16 starter skill set). Stub: no live
broker/options data feed is wired yet.
"""


from typing import Any
def option_chain_read(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "underlying": params.get("underlying"),
        "legs": [],
        "note": "stub: no live option chain feed wired yet",
    }
