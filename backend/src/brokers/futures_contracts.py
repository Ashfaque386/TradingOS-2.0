"""Picking the monthly future a paper subscription should trade (real-market-data plan, phase 4).

Pure rules plus one broker lookup, kept below the data and orchestration layers so both can use
them.

- The contract is the soonest one expiring **more than `roll_days` days from today**, so a
  subscription is never put into a contract that is about to be rolled out of again.
- A contract is *due for a roll* from `roll_days` days before its expiry.
- Contracts and lot sizes come from the connected brokers' instrument masters, first connected
  broker first; a broker that fails is skipped and the next tried. Nothing is guessed: with no
  answer, the error says why.
"""

from collections.abc import Sequence
from datetime import date

from src.brokers.base import BrokerAdapter, FuturesContract


class NoFuturesContractError(Exception):
    """No connected broker lists a usable contract for the underlying."""


def select_contract(
    contracts: Sequence[FuturesContract], *, today: date, roll_days: int
) -> FuturesContract | None:
    """The soonest contract that expires more than `roll_days` days after `today`."""
    for contract in sorted(contracts, key=lambda c: c.expiry):
        if (contract.expiry - today).days > roll_days:
            return contract
    return None


def roll_due(expiry: date, *, today: date, roll_days: int) -> bool:
    """True from `roll_days` days before expiry (and after it): time to move to the next one."""
    return (expiry - today).days <= roll_days


async def resolve_contract(
    adapters: Sequence[BrokerAdapter], underlying: str, *, today: date, roll_days: int
) -> FuturesContract:
    """The contract a new or rolling subscription should trade."""
    problems: list[str] = []
    for adapter in adapters:
        try:
            contracts = await adapter.get_futures_contracts(underlying)
        except Exception as exc:  # noqa: BLE001 - one broker failing must not stop the next
            problems.append(f"{adapter.broker_name}: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        chosen = select_contract(contracts, today=today, roll_days=roll_days)
        if chosen is not None:
            return chosen
        problems.append(f"{adapter.broker_name}: no contract expiring after {roll_days} days")
    detail = "; ".join(problems) if problems else "no broker is connected"
    raise NoFuturesContractError(f"no futures contract for {underlying} ({detail})")
