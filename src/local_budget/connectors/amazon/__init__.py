"""Amazon connector — item-level detail behind an opaque `AMAZON MKTPL` charge.

Layered so exactly one module touches the network:

    session.py  credentials + hardened cookie jar
    fetch.py    the ONLY network boundary
    store.py    entities -> integer-cent rows
    match.py    reconcile against the bank ledger
    sync.py     orchestration (the only module that needs all four)

See `session.py` for the standing trade-offs: Amazon publishes no consumer
order API, so this parses the consumer site, and that has ToS and durability
consequences that are properties of the approach rather than defects in it.
"""
from . import fetch, match, session, store, sync   # noqa: F401
