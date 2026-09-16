"""Options legs grounding + naked-options scan (Build Spec §9, §8).

**Grounding**: the Options Strategy Agent's proposed legs must be checked
against a live option chain before acceptance -- every leg has to resolve
to a strike that actually exists on the chain, annotated with the chain's
own market data (LTP) rather than trusting whatever an LLM proposed.
`mock_option_chain()` is a deterministic, dependency-free stand-in for a
live feed (the real one is a Phase 8 broker adapter / Phase 10 data
ingestion concern) -- same honest-stub posture as every other
not-yet-built external integration in this codebase.

**Naked-options scan**: Build Spec §8's stated rule -- "every sold option
leg must have a same-underlying OTM hedge" -- implemented for real here
rather than as a no-op stub, since the rule as stated is simple enough to
apply exactly. What's genuinely deferred to Phase 6 (Risk & Safety Layer)
is everything else that phase owns: broker margin checks, the correlation
constraint, and wiring this same scan into the live/paper per-tick risk
gate in addition to this pre-deployment check (Build Spec §8 requires
both; this module is only the pre-deployment half).
"""

from dataclasses import dataclass, field


def mock_option_chain(underlying: str, expiry: str) -> dict:
    atm = 20000 if underlying.upper() == "NIFTY" else 45000
    strikes = [atm + i * 100 for i in range(-10, 11)]
    return {
        "underlying": underlying,
        "expiry": expiry,
        "atm_strike": atm,
        "strikes": {
            strike: {
                "ce_ltp": round(max(atm + 500 - strike, 1) / 10, 2),
                "pe_ltp": round(max(strike - atm + 500, 1) / 10, 2),
            }
            for strike in strikes
        },
    }


@dataclass(frozen=True, slots=True)
class GroundedLeg:
    option_type: str  # "CE" | "PE"
    strike: float
    expiry: str
    action: str  # "buy" | "sell"
    qty: int
    ltp: float
    grounded: bool


def ground_option_legs(
    legs: list[dict], *, underlying: str, expiry: str, chain: dict | None = None
) -> list[GroundedLeg]:
    chain = chain or mock_option_chain(underlying, expiry)
    grounded: list[GroundedLeg] = []
    for leg in legs:
        strike = leg["strike"]
        option_type = leg["option_type"].upper()
        chain_entry = chain["strikes"].get(strike)
        if chain_entry is None:
            grounded.append(
                GroundedLeg(
                    option_type=option_type,
                    strike=strike,
                    expiry=expiry,
                    action=leg["action"],
                    qty=leg["qty"],
                    ltp=0.0,
                    grounded=False,
                )
            )
            continue
        ltp = chain_entry["ce_ltp"] if option_type == "CE" else chain_entry["pe_ltp"]
        grounded.append(
            GroundedLeg(
                option_type=option_type,
                strike=strike,
                expiry=expiry,
                action=leg["action"],
                qty=leg["qty"],
                ltp=ltp,
                grounded=True,
            )
        )
    return grounded


@dataclass(frozen=True, slots=True)
class NakedOptionsScanResult:
    blocked: bool
    reasons: list[str] = field(default_factory=list)


def naked_options_scan(legs: list[GroundedLeg]) -> NakedOptionsScanResult:
    """A sold leg is "hedged" if there's a bought leg of the same
    option_type strictly further out-of-the-money -- for a sold call, a
    bought call at a higher strike; for a sold put, a bought put at a
    lower strike (the textbook vertical-spread shape). A leg that couldn't
    be grounded against the chain at all is treated as unresolved and
    blocks acceptance too -- Build Spec §9 requires grounding before
    acceptance, so an ungrounded leg can't be waved through here either.
    """
    reasons: list[str] = []
    for leg in legs:
        if not leg.grounded:
            reasons.append(
                f"leg {leg.option_type} {leg.strike} could not be grounded against the option chain"
            )
            continue
        if leg.action != "sell":
            continue
        hedged = any(
            other.grounded
            and other.action == "buy"
            and other.option_type == leg.option_type
            and (
                (leg.option_type == "CE" and other.strike > leg.strike)
                or (leg.option_type == "PE" and other.strike < leg.strike)
            )
            for other in legs
        )
        if not hedged:
            reasons.append(
                f"sold {leg.option_type} {leg.strike} has no same-underlying OTM hedge (naked)"
            )
    return NakedOptionsScanResult(blocked=bool(reasons), reasons=reasons)
