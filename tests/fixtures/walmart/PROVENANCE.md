# Walmart fixtures

**Synthetic payloads with a real shape.** Field names, nesting, key spelling,
types and value formats were transcribed from live captures of one Walmart
account; every name, price, product, order id and date is invented.

| file | reproduces |
|---|---|
| `orders-list.json` | `__NEXT_DATA__` of `walmart.com/orders` |
| `order-detail.json` | `__NEXT_DATA__` of `walmart.com/orders/<id>` |
| `orders-list.html` / `order-detail.html` | the same blobs inside a page, for `parse.next_data` |

## Why they are synthetic

The Amazon fixtures next door are real HTML, vendored from the `amazon-orders`
project's MIT-licensed test suite — someone else's demo account, no PII. Walmart
has no upstream project to borrow from, so the only real payloads available are
the repository owner's own order history: names, addresses, and a line-by-line
record of what a household buys. This repository is public.

So the captures stay in gitignored `data/walmart/capture/`, and what is
committed reproduces their **structure** with invented contents.

## What they can and cannot catch

They pin the contract: a renamed field, a changed nesting level, a type flip, a
price that stops being a string. That is what `test_walmart_contract.py` runs
the real parser against, and it is the tier that would have caught both
corrections this connector needed —

- `priceInfo.linePrice` is a LINE total, not a unit price (two of a thing is one
  line reading $14.50), and
- only the LIST page carries `isInStore`, so a detail fetch must not blank the
  channel.

They cannot tell us the scraper works against Walmart today. Only a live
`budget walmart sync` does that.

## Shapes worth knowing, all verified against real payloads

- Order data is server-rendered into `__NEXT_DATA__`; the list page needs no
  XHR, and the detail page hydrates from `props.pageProps.initialData`.
- The detail page's item array is `groups_2101` — a VERSIONED key. `parse.py`
  finds it by prefix for that reason.
- `chargeHistory` is a title and a message with `args: null`. Walmart publishes
  no per-charge record here, which is why `match.py` sums bank rows instead.
- `paymentMethods` separates card from Walmart Cash, and the two do not
  necessarily sum to the order total.
- A group's `categories` is `[{"type": "REGULAR"}]` — a fulfilment flag, not a
  product taxonomy. Walmart publishes no shelf category on these pages, so
  `walmart_items.category` is NULL in practice and the report's keyword
  heuristic carries the grouping.

## Refreshing

Run `budget walmart capture`, read `data/walmart/capture/*-MANIFEST.json`, and
hand-edit these files to match any shape change. Do not paste a real capture in.
