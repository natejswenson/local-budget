"""Walmart connector — item-level detail behind an opaque `WALMART.COM` charge.

Same problem as the Amazon connector, and larger: `WALMART.COM $84.31` says
nothing about what was bought, and a household can spend more at Walmart than at Amazon.

Layered the same way, so exactly one module touches the network:

    session.py  the captured browser session, hardened on disk
    fetch.py    the ONLY network boundary
    parse.py    raw page payloads -> plain entity dicts
    store.py    entities -> integer-cent rows
    match.py    reconcile orders against SETS of bank rows
    sync.py     orchestration

**Two differences from the Amazon connector, both structural.**

1. *There is no upstream library.* Amazon rides on `amazon-orders`; nothing
   equivalent exists for Walmart consumer orders, so the parser is ours and
   `parse.py` is a layer the Amazon package does not need. Upstream fixes are
   not the maintenance story here — we are.

2. *An order is not a charge.* Amazon publishes its own list of card charges at
   exactly the granularity the bank posts them. Walmart publishes orders — and
   an order routinely settles as SEVERAL partial charges it never enumerates.
   One real order became five bank rows. So `match.py` recovers the
   settlement by subset-sum: the set of unmatched Walmart bank rows near the
   order date that sums to its total exactly, accepted only when that set is
   unique.

See `session.py` for the standing trade-offs, which are properties of the
approach rather than defects in it.

Submodules are NOT re-exported here. Half of them reach for Playwright, and
importing this package must not drag a browser driver into a process that only
wanted to read a coverage number.
"""
