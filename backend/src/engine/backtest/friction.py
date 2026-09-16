"""Indian cash-equity trading cost model (Build Spec §10): brokerage, STT,
exchange transaction charges, the SEBI turnover fee, stamp duty, GST, and
dynamic ATR/volume-based slippage.

Default rates are the commonly-published current NSE/SEBI schedule for a
discount-broker-style cash-equity account (zero delivery brokerage, capped
intraday brokerage) -- real, current figures, not an invented model, but
also not a guarantee against future rate changes; every rate is a
`FrictionModel` field so a caller can override any of them without editing
this module. Each cost component is computed and returned separately
(`LegCosts`), not just summed, specifically so a hand calculation can
verify each line item against the same published rates rather than only
checking a total.

- **Brokerage**: intraday capped at the lower of 0.03% of turnover or ₹20
  per executed leg; delivery is free (0%).
- **STT** (Securities Transaction Tax): delivery charges 0.1% on *both*
  the buy and sell leg; intraday charges 0.025% on the sell leg only.
- **Exchange transaction charges**: 0.00297% of turnover on both legs
  (NSE's published cash-segment rate).
- **SEBI turnover fee**: ₹10 per crore of turnover (0.0001%) on both legs.
- **Stamp duty**: charged on the buy leg only (SEBI's post-July-2020
  standardized schedule) -- 0.015% for delivery, 0.003% for intraday.
- **GST**: 18% on (brokerage + exchange transaction charges) only -- STT,
  the SEBI fee, and stamp duty are statutory levies GST is not charged on
  top of.

Slippage is modeled as a per-share price adjustment with two additive
components, both configurable: an ATR-based component
(`atr_multiplier * atr`, capturing that a more volatile instrument is
harder to execute at the quoted price) and a volume-based component
(`volume_impact_coefficient * (quantity / avg_daily_volume) * price`,
capturing that a large order relative to typical daily volume moves the
price against the trader). This is a documented modeling choice, not a
published regulatory figure like the items above -- Build Spec §10 asks
for "dynamic ATR/volume-based slippage" without prescribing an exact
formula.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FrictionModel:
    intraday_brokerage_pct: float = 0.0003
    intraday_brokerage_cap: float = 20.0
    delivery_brokerage_pct: float = 0.0
    delivery_brokerage_cap: float = 0.0

    stt_delivery_pct: float = 0.001
    stt_intraday_sell_pct: float = 0.00025

    exchange_txn_pct: float = 0.0000297
    sebi_fee_pct: float = 0.000001

    stamp_duty_delivery_buy_pct: float = 0.00015
    stamp_duty_intraday_buy_pct: float = 0.00003

    gst_pct: float = 0.18

    atr_slippage_multiplier: float = 0.1
    volume_impact_coefficient: float = 0.1


@dataclass(frozen=True, slots=True)
class LegCosts:
    brokerage: float
    stt: float
    exchange_txn_charges: float
    sebi_fee: float
    stamp_duty: float
    gst: float

    @property
    def total(self) -> float:
        return (
            self.brokerage
            + self.stt
            + self.exchange_txn_charges
            + self.sebi_fee
            + self.stamp_duty
            + self.gst
        )


def _brokerage(turnover: float, pct: float, cap: float) -> float:
    if pct <= 0:
        return 0.0
    uncapped = turnover * pct
    if cap <= 0:
        return uncapped
    return min(uncapped, cap)


def compute_leg_costs(
    *, turnover: float, is_buy: bool, is_delivery: bool, model: FrictionModel = FrictionModel()
) -> LegCosts:
    """turnover = price * quantity for this one leg (entry or exit)."""
    if is_delivery:
        brokerage = _brokerage(turnover, model.delivery_brokerage_pct, model.delivery_brokerage_cap)
        stt = turnover * model.stt_delivery_pct
        stamp_duty = turnover * model.stamp_duty_delivery_buy_pct if is_buy else 0.0
    else:
        brokerage = _brokerage(turnover, model.intraday_brokerage_pct, model.intraday_brokerage_cap)
        stt = 0.0 if is_buy else turnover * model.stt_intraday_sell_pct
        stamp_duty = turnover * model.stamp_duty_intraday_buy_pct if is_buy else 0.0

    exchange_txn_charges = turnover * model.exchange_txn_pct
    sebi_fee = turnover * model.sebi_fee_pct
    gst = model.gst_pct * (brokerage + exchange_txn_charges)

    return LegCosts(
        brokerage=brokerage,
        stt=stt,
        exchange_txn_charges=exchange_txn_charges,
        sebi_fee=sebi_fee,
        stamp_duty=stamp_duty,
        gst=gst,
    )


def compute_slippage_per_share(
    *,
    atr: float,
    quantity: float,
    avg_daily_volume: float,
    price: float,
    model: FrictionModel = FrictionModel(),
) -> float:
    atr_component = model.atr_slippage_multiplier * atr
    volume_component = (
        model.volume_impact_coefficient * (quantity / avg_daily_volume) * price
        if avg_daily_volume > 0
        else 0.0
    )
    return atr_component + volume_component
