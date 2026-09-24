"""Broker Integration Layer (Build Spec §13): a single `BrokerAdapter`
interface with concrete Zerodha Kite Connect and Upstox implementations,
a `BrokerCircuitBreaker`-wrapping decorator (Phase 6 reused, not
reimplemented), an encrypted credential lookup, and Shadow Mode dry-run
checks. Implemented in Phase 8.
"""
