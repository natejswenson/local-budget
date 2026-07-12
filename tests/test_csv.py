"""CSV parsing — headerless bank-export format + headered variants."""
from __future__ import annotations

import pytest

from local_budget import db
from local_budget.ingest import importer, parse


def _write(tmp_path, text, name="stmt.csv"):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_headerless_csv(data_dir, tmp_path):
    # Real headerless "download account activity" shape: no header, description LAST col.
    csv = (
        '"06/03/2024","-52.40","*","","WALMART STORE 1234"\n'
        '"06/05/2024","-23.40","*","","CHIPOTLE MEXICAN GRILL"\n'
        '"06/01/2024","2000.00","*","","ACME CORP PAYROLL"\n'
    )
    db.init_schema()
    importer.import_file(_write(tmp_path, csv))
    with db.connect() as conn:
        rows = {r["merchant_norm"]: r["amount_cents"]
                for r in conn.execute("SELECT merchant_norm, amount_cents FROM transactions").fetchall()}
    assert rows["WALMART STORE 1234"] == -5240
    assert rows["CHIPOTLE MEXICAN GRILL"] == -2340
    assert rows["ACME CORP PAYROLL"] == 200000
    assert "UNKNOWN" not in rows   # description parsed, not collapsed


def test_headered_csv_description_column(data_dir, tmp_path):
    csv = (
        "Date,Amount,Description\n"
        "06/03/2024,-52.40,WALMART STORE\n"
        "06/05/2024,-23.40,CHIPOTLE\n"
    )
    db.init_schema()
    importer.import_file(_write(tmp_path, csv))
    with db.connect() as conn:
        names = {r[0] for r in conn.execute("SELECT merchant_norm FROM transactions").fetchall()}
    assert names == {"WALMART STORE", "CHIPOTLE"}


def test_headered_csv_payee_column(data_dir, tmp_path):
    csv = "date,amount,payee,memo\n2024-06-03,-10.00,SHELL OIL,fuel\n"
    db.init_schema()
    importer.import_file(_write(tmp_path, csv))
    with db.connect() as conn:
        r = conn.execute("SELECT merchant_norm, category FROM transactions").fetchone()
    assert r["merchant_norm"] == "SHELL OIL"
    assert r["category"] == "Transportation"   # builtin rule still fires


def test_csv_distinct_merchants_preserved(data_dir, tmp_path):
    # Regression for the all-UNKNOWN bug: distinct descriptions stay distinct.
    csv = "".join(f'"06/0{i}/2024","-{i}.00","*","","MERCHANT {i}"\n' for i in range(1, 6))
    db.init_schema()
    importer.import_file(_write(tmp_path, csv))
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(DISTINCT merchant_norm) FROM transactions").fetchone()[0]
    assert n == 5


def test_empty_csv_raises(data_dir, tmp_path):
    with pytest.raises(parse.ParseError):
        parse.parse_file(_write(tmp_path, "\n\n"))
