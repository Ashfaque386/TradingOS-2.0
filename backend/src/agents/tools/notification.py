"""notification-send (Build Spec §16 starter skill set). Explicitly allowed
to stub until Phase 12 (Notifications & Omni-Channel, Build Spec §18) --
per the Phase 3 build prompt.
"""


def notification_send(params: dict) -> dict:
    return {
        "sent": False,
        "channel": params.get("channel"),
        "note": "stub until Phase 12 (Notifications & Omni-Channel)",
    }
