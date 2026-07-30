"""Walmart connector — item-level detail behind an opaque `WALMART.COM` charge.

Same problem as the Amazon connector, and larger: `WALMART.COM $84.31` says
nothing about what was bought, and a household can spend more at Walmart than at Amazon.

Layered the same way, so exactly one module touches the network:

    session.py  the captured browser session, hardened on disk
    fetch.py    the ONLY network boundary
    parse.py    raw page payloads -> plain entity dicts
    store.py    entities -> integer-cent rows
    match.py    reconcile against the bank ledger
    sync.py     orchestration

**Two differences from the Amazon connector, both structural.**

1. *There is no upstream library.* Amazon rides on `amazon-orders`; nothing
   equivalent exists for Walmart consumer orders, so the parser is ours and
   `parse.py` is a layer the Amazon package does not need. Upstream fixes are
   not the maintenance story here — we are.

2. *There is no charge list.* Amazon publishes its own list of card charges, at
   exactly the granularity the bank posts them. Walmart publishes orders. An
   order still settles as one **or several** charges, so `walmart_charges`
   exists as the reconciliation key either way: filled from the order's payment
   lines when they are available, and otherwise synthesized from the order total
   and flagged `derived = 1` so no report can mistake an inference for an
   observation.

See `session.py` for the standing trade-offs, which are properties of the
approach rather than defects in it.

Submodules are NOT re-exported here. Half of them reach for Playwright, and
importing this package must not drag a browser driver into a process that only
wanted to read a coverage number.
"""
