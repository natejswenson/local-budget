"""Walmart connector — item-level detail behind an opaque `WALMART.COM` charge.

Same problem as the Amazon connector, and larger: `WALMART.COM $84.31` says
nothing about what was bought, and a household can spend more at Walmart than at
Amazon.

**Ingestion is a FILE, not a scrape**, and that is the connector's defining
difference from Amazon's. There is no consumer order API, and reading the site
through a captured browser session did work — right up until PerimeterX
challenged the third page of pagination, every time, which made backfilling a
year of history structurally impossible. That whole path is gone. Orders now
arrive as a purchase-history spreadsheet exported by hand, which has no bot
surface at all because it is a file.

    import_xlsx.py  the export -> plain entity dicts, then store + match
    store.py        entities -> integer-cent rows
    match.py        reconcile orders against SETS of bank rows
    split.py        an order's lines, scaled to one bank charge
    report.py       the standalone Walmart PDF

Nothing here signs in, holds a session, or makes a request.

**An order is still not a charge.** Amazon publishes its own list of card
charges at exactly the granularity the bank posts them. Walmart publishes
orders — and an order routinely settles as SEVERAL partial charges it never
enumerates. One real order became five bank rows. So `match.py` recovers the
settlement by subset-sum: the set of unmatched Walmart bank rows near the order
date that sums to its total exactly, accepted only when that set is unique.

Submodules are NOT re-exported here, so importing this package stays cheap for a
caller that only wanted to read a coverage number.
"""
