"""Fabricated OFX/QFX builders for tests. NEVER derived from real statements."""
from __future__ import annotations

from pathlib import Path

_HEADER = """OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE
"""


def _txn(trntype, dtposted, amount, fitid, name, memo=None):
    parts = [
        "<STMTTRN>",
        f"<TRNTYPE>{trntype}",
        f"<DTPOSTED>{dtposted}",
        f"<TRNAMT>{amount}",
        f"<FITID>{fitid}",
        f"<NAME>{name}",
    ]
    if memo:
        parts.append(f"<MEMO>{memo}")
    parts.append("</STMTTRN>")
    return "\n".join(parts)


def build_ofx(txns, *, bankid="121000248", acctid="1234567890", acct_type="CHECKING") -> str:
    """txns: list of dicts with keys trntype, dtposted (YYYYMMDD), amount, fitid, name[, memo]."""
    body = "\n".join(_txn(**t) for t in txns)
    return f"""{_HEADER}
<OFX>
<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS><DTSERVER>20260601120000<LANGUAGE>ENG</SONRS></SIGNONMSGSRSV1>
<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STATUS><CODE>0<SEVERITY>INFO</STATUS>
<STMTRS><CURDEF>USD<BANKACCTFROM><BANKID>{bankid}<ACCTID>{acctid}<ACCTTYPE>{acct_type}</BANKACCTFROM>
<BANKTRANLIST><DTSTART>20260601<DTEND>20260630
{body}
</BANKTRANLIST>
<LEDGERBAL><BALAMT>1000.00<DTASOF>20260630</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
"""


def write_ofx(path: Path, txns, **kw) -> Path:
    path.write_text(build_ofx(txns, **kw))
    return path
