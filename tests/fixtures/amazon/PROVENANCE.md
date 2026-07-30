# Amazon HTML fixtures

Real Amazon page snapshots, vendored from the `amazon-orders` project's own test
suite (MIT, © Alex Laird) at `tests/resources/`:

| file | upstream name |
|---|---|
| `order-snippet.html` | `orders/order-currency-stripped-snippet.html` |
| `transaction-charge.html` | `transactions/transaction-snippet.html` |
| `transaction-refund.html` | `transactions/transaction-refund-snippet.html` |

## Why these exist

`tests/test_amazon_connector.py` mocks at the `fetch` boundary with hand-built
fakes. That is fast and it tests the matcher thoroughly — but it tests this
code against **our assumptions about the library's objects**, and every one of
those tests would still pass if the assumptions were wrong.

They were. Parsing a real fixture for the first time showed that
`Order.grand_total` is **positive** while `Transaction.grand_total` is
**negative for a charge** — opposite conventions the fakes had both wrong, and
that no amount of mock-based testing could have surfaced.

`tests/test_amazon_contract.py` runs the **real parser** over these files and
feeds the resulting **real entity objects** through `store` and `match`. Still
offline, still no credentials, but it pins the actual contract with upstream:
a renamed field, a changed type, or a flipped sign fails here.

## Refreshing

```bash
BASE=https://raw.githubusercontent.com/alexdlaird/amazon-orders/main/tests/resources
curl -sL "$BASE/orders/order-currency-stripped-snippet.html"      -o order-snippet.html
curl -sL "$BASE/transactions/transaction-snippet.html"            -o transaction-charge.html
curl -sL "$BASE/transactions/transaction-refund-snippet.html"     -o transaction-refund.html
```

Worth doing after any `amazon-orders` upgrade: these fixtures move with the
parser, and a stale pair can hide a real break just as easily as it catches one.
