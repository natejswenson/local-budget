"""Parse OFX/QFX (and a small CSV fallback) into normalized records.

`ofxparse` handles Quicken QFX (the Intuit tags) fine. We pull the raw amount
through `str(txn.amount)` — ofxparse builds `amount` as a `Decimal` from the
TRNAMT string, so `str()` is the exact decimal string our cents converter
requires (it then RAISES on >2 decimals; §3/S5). We never touch a float.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ParsedTxn:
    fitid: str
    posted_date: str          # ISO YYYY-MM-DD
    amount_str: str           # raw decimal STRING (never a float)
    txn_type: str | None
    payee: str | None
    memo: str | None
    # CSV has no stable bank id; the importer computes a CONTENT-ONLY synthetic
    # FITID (account, date, cents, merchant_norm, per-key occurrence ordinal) —
    # never a row index (design §dedup, red-team F1). True only for CSV rows.
    synthetic_fitid: bool = False


@dataclass
class ParsedAccount:
    bankid: str               # routing number / bank id
    acctid: str               # account number (full — masked at import, never stored)
    acct_type: str | None
    institution: str | None
    txns: list[ParsedTxn] = field(default_factory=list)
    # Count of <STMTTRN> rows ofxparse discarded that we could NOT recover (e.g.
    # malformed amount/date — NOT a FITID-less row). These never enter `txns`, so
    # the importer surfaces this count NON-SILENTLY rather than disposing the file
    # with the charge lost (red-team F1). Good rows still import.
    dropped_unparseable: int = 0


class ParseError(ValueError):
    pass


def parse_file(path: Path) -> list[ParsedAccount]:
    suffix = path.suffix.lower()
    if suffix in (".ofx", ".qfx", ".qbo"):
        return _parse_ofx(path)
    if suffix == ".csv":
        return _parse_csv(path)
    # OFX/QFX often arrive with odd extensions; sniff the content.
    head = path.read_bytes()[:512].lstrip()
    if head[:5].upper() == b"OFXHE" or b"<OFX>" in head.upper():
        return _parse_ofx(path)
    raise ParseError(f"unrecognized file type: {path.name}")


def _parse_ofx(path: Path) -> list[ParsedAccount]:
    from ofxparse import OfxParser  # imported lazily; deterministic path opens no socket

    # fail_fast=False so ONE bad <STMTTRN> does not drop the whole file (red-team
    # F-3). A real WF OFX export can include FITID-less rows; ofxparse treats a
    # missing <FITID> as a discarded entry rather than parsing it. We import every
    # good transaction AND recover the FITID-less discards below. A discard we
    # CANNOT recover (malformed amount/date — not just a missing FITID) is COUNTED
    # into `dropped_unparseable` so the importer surfaces it (CLI + dashboard)
    # instead of silently disposing the file with that charge lost (red-team F1).
    with path.open("rb") as fh:
        try:
            ofx = OfxParser.parse(fh, fail_fast=False)
        except Exception as e:  # noqa: BLE001
            raise ParseError(f"could not parse OFX/QFX: {e}") from e

    out: list[ParsedAccount] = []
    for acct in ofx.accounts:
        pa = ParsedAccount(
            bankid=str(getattr(acct, "routing_number", "") or ""),
            acctid=str(getattr(acct, "account_id", "") or ""),
            acct_type=(getattr(acct, "account_type", "") or "").upper() or None,
            institution=_institution(acct),
        )
        stmt = acct.statement
        for t in stmt.transactions:
            # Rows WITH a real bank FITID keep it (synthetic_fitid=False).
            pa.txns.append(ParsedTxn(
                fitid=str(t.id),
                synthetic_fitid=False,
                posted_date=t.date.date().isoformat(),
                amount_str=str(t.amount),     # exact decimal string from ofxparse
                txn_type=(t.type or "").upper() or None,
                payee=t.payee or None,
                memo=t.memo or None,
            ))
        # Recover FITID-less transactions ofxparse discarded (red-team F-3). Each
        # gets synthetic_fitid=True so the importer derives the SAME account-scoped,
        # occurrence-ordinal CONTENT FITID it uses for CSV — dedups correctly across
        # re-downloads. ONLY a "Missing FIT id" discard is recovered. Any OTHER
        # discard (malformed amount/date) is COUNTED into dropped_unparseable so the
        # importer surfaces the loss non-silently (red-team F1) — never disposed quietly.
        for entry in getattr(stmt, "discarded_entries", []) or []:
            t = _recover_fitidless_txn(entry)
            if t is not None:
                pa.txns.append(t)
            else:
                pa.dropped_unparseable += 1
        out.append(pa)
    if not out:
        raise ParseError("no accounts found in file")
    return out


def _recover_fitidless_txn(entry: dict) -> ParsedTxn | None:
    """Turn an ofxparse discarded <STMTTRN> that lacked only a <FITID> into a
    synthetic-FITID ParsedTxn (red-team F-3). Returns None for any other discard
    reason (malformed amount/date); the caller COUNTS those Nones into
    dropped_unparseable so the loss is surfaced (CLI + dashboard), not silent (F1)."""
    if "FIT id" not in str(entry.get("error", "")):
        return None   # not a FITID-less discard → leave it to error out
    el = entry.get("content")
    if el is None:
        return None
    amt = _tag_text(el, "trnamt")
    dtp = _tag_text(el, "dtposted")
    if amt is None or dtp is None:
        return None   # truly malformed → not safe to import; let it surface
    try:
        posted = _ofx_date(dtp)
    except ParseError:
        return None
    return ParsedTxn(
        fitid="", synthetic_fitid=True,
        posted_date=posted, amount_str=amt.strip(),
        txn_type=(_tag_text(el, "trntype") or "").upper() or None,
        payee=(_tag_text(el, "name") or None),
        memo=(_tag_text(el, "memo") or None),
    )


def _tag_text(el, name: str) -> str | None:  # noqa: ANN001
    """First text content of <name> inside an ofxparse-discarded element, or None."""
    tag = el.find(name)
    if tag is None or not getattr(tag, "contents", None):
        return None
    return str(tag.contents[0]).strip() or None


def _ofx_date(raw: str) -> str:
    """OFX DTPOSTED (YYYYMMDD[HHMMSS][.xxx][tz]) → ISO YYYY-MM-DD."""
    digits = raw.strip()[:8]
    from datetime import datetime
    try:
        return datetime.strptime(digits, "%Y%m%d").date().isoformat()
    except ValueError as e:
        raise ParseError(f"unrecognized OFX date: {raw!r}") from e


def _institution(acct) -> str | None:  # noqa: ANN001
    inst = getattr(acct, "institution", None)
    name = getattr(inst, "organization", None) if inst else None
    return name or "Wells Fargo"


_DESC_HEADERS = ("description", "payee", "name", "merchant", "memo", "transaction", "details")
_DATE_HEADERS = ("date", "posted date", "transaction date", "post date")
_AMOUNT_HEADERS = ("amount", "amount debit credit", "amount (usd)")


def _parse_csv(path: Path) -> list[ParsedAccount]:
    """CSV parser handling BOTH Wells Fargo's HEADERLESS export and headered CSVs.

    Wells Fargo "Download Account Activity" CSV has NO header row and 5 columns:
        Date, Amount, "*", "", Description   e.g.  06/03/2024,-52.40,*,,WALMART STORE
    so the description is the LAST column. Headered CSVs match flexible column
    names (date/amount + description/payee/name/...). A synthetic FITID is derived
    (no stable id in CSV → weaker dedup; design D2).
    """
    rows = [r for r in csv.reader(path.open(newline="")) if any(c.strip() for c in r)]
    if not rows:
        raise ParseError("empty CSV")

    headerless = _looks_like_date(rows[0][0]) and _looks_like_amount(rows[0][1] if len(rows[0]) > 1 else "")
    if headerless:
        i_date, i_amt, i_desc, i_memo, i_type = 0, 1, len(rows[0]) - 1, None, None
        data = rows
    else:
        header = [c.lower().strip() for c in rows[0]]
        i_date = _find(header, _DATE_HEADERS)
        i_amt = _find(header, _AMOUNT_HEADERS)
        i_desc = _find(header, _DESC_HEADERS)
        i_memo = _find(header, ("memo", "notes"))
        i_type = _find(header, ("type", "transaction type"))
        if i_date is None or i_amt is None:
            raise ParseError("CSV must have a date and an amount column")
        if i_desc == i_amt or i_desc == i_date:
            i_desc = None
        data = rows[1:]

    # DEFERRED FEATURE — single-account CSV limitation (red-team F-2): all CSV rows
    # share ONE synthetic "csv" account (bankid/acctid both "csv"), because a WF
    # CSV export carries no account number. Consequence: two DIFFERENT accounts both
    # exported as CSV can cross-dedup a coincidentally-identical (date, amount,
    # merchant) row. Per the product owner's decision this is NOT fixed here; the
    # multi-account-safe path is OFX/QFX, which carry real account numbers. The
    # limitation is surfaced to the user in `budget set-inbox`/`budget intake` and
    # the dashboard intake area rather than being silent.
    pa = ParsedAccount(bankid="csv", acctid="csv", acct_type=None, institution="Wells Fargo")
    for i, row in enumerate(data):
        if len(row) <= i_amt:
            continue
        date = _iso_date(row[i_date].strip())
        amount = row[i_amt].strip().replace("$", "")
        desc = row[i_desc].strip() if (i_desc is not None and len(row) > i_desc) else None
        memo = row[i_memo].strip() if (i_memo is not None and len(row) > i_memo) else None
        ttype = row[i_type].strip().upper() if (i_type is not None and len(row) > i_type) else None
        # No FITID here: the importer computes a content-only synthetic FITID
        # (account-scoped, with a per-key occurrence ordinal) — NOT the row index.
        pa.txns.append(ParsedTxn(
            fitid="", synthetic_fitid=True,
            posted_date=date, amount_str=amount, txn_type=ttype or None,
            payee=desc or None, memo=memo or None,
        ))
    if not pa.txns:
        raise ParseError("no transactions parsed from CSV")
    return [pa]


def _find(header: list[str], names: tuple[str, ...]) -> int | None:
    for n in names:
        if n in header:
            return header.index(n)
    return None


def _looks_like_date(s: str) -> bool:
    try:
        _iso_date(s.strip())
        return True
    except ParseError:
        return False


def _looks_like_amount(s: str) -> bool:
    s = s.strip().replace("$", "").replace(",", "")
    if not s:
        return False
    try:
        float(s)
        return True
    except ValueError:
        return False


def _iso_date(s: str) -> str:
    from datetime import datetime
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    raise ParseError(f"unrecognized date: {s!r}")
