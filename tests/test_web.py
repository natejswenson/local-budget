"""Web dashboard API (FastAPI TestClient — no network, no PII to the browser)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from local_budget import db
from local_budget.ingest import importer
from local_budget.web import server

from ofx_fixtures import write_ofx


@pytest.fixture(autouse=True)
def no_network_egress():
    """TestClient drives ASGI in-process (needs the socket machinery); no real I/O."""
    yield


@pytest.fixture
def client(data_dir, tmp_path):
    db.init_schema()
    importer.import_file(write_ofx(tmp_path / "stmt.qfx", [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-50.00", "fitid": "G1",
         "name": "WALMART ACCT 4111222233334444"},
        {"trntype": "CREDIT", "dtposted": "20260601", "amount": "2000.00", "fitid": "I1", "name": "ACME PAYROLL"},
    ]))
    return TestClient(server.create_app(), base_url="http://127.0.0.1")


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "buddy" in r.text


def test_dashboard_surfaces_single_account_csv_note(client):
    # F-2 (deferred feature made NON-SILENT): the dashboard intake area must warn
    # that CSV is treated as a single account and point to OFX/QFX for multi-account.
    text = client.get("/").text
    assert "single account" in text.lower()
    assert "OFX" in text or "QFX" in text


def test_dashboard_surfaces_dropped_rows(client):
    # F1 (dashboard surface): the intake area must render a non-silent banner for
    # malformed/unrecoverable rows that were NOT imported (good rows still import).
    text = client.get("/").text
    assert "dropped-bar" in text
    assert "showDropped" in text
    assert "could not be read" in text


def test_intake_run_payload_reports_dropped_rows(client, tmp_path, monkeypatch):
    # F1 (dashboard data): a malformed OFX row → /api/intake/run reports
    # dropped_rows>=1 with the good row imported. No filename / row content in payload.
    from local_budget import db, inbox_adapter
    box = tmp_path / "inbox"
    box.mkdir()
    db.set_setting("inbox_dir", str(box))
    monkeypatch.setattr(inbox_adapter, "STABILITY_SECS", 0)
    (box / "stmt.ofx").write_text(
        "OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\nSECURITY:NONE\nENCODING:USASCII\n"
        "CHARSET:1252\nCOMPRESSION:NONE\nOLDFILEUID:NONE\nNEWFILEUID:NONE\n\n<OFX>\n"
        "<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS>"
        "<DTSERVER>20260601120000<LANGUAGE>ENG</SONRS></SIGNONMSGSRSV1>\n"
        "<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STATUS><CODE>0<SEVERITY>INFO</STATUS>\n"
        "<STMTRS><CURDEF>USD<BANKACCTFROM><BANKID>121000248<ACCTID>1234567890"
        "<ACCTTYPE>CHECKING</BANKACCTFROM>\n"
        "<BANKTRANLIST><DTSTART>20260601<DTEND>20260630\n"
        "<STMTTRN>\n<TRNTYPE>DEBIT\n<DTPOSTED>20260603\n<TRNAMT>-77.00\n<FITID>DROP_GOOD\n<NAME>COSTCO\n</STMTTRN>\n"
        "<STMTTRN>\n<TRNTYPE>DEBIT\n<DTPOSTED>20260604\n<TRNAMT>NOTANUMBER\n<FITID>DROP_BAD\n<NAME>SHELL\n</STMTTRN>\n"
        "</BANKTRANLIST>\n<LEDGERBAL><BALAMT>1000.00<DTASOF>20260630</LEDGERBAL>\n"
        "</STMTRS></STMTTRNRS></BANKMSGSRSV1>\n</OFX>\n")
    run = client.post("/api/intake/run").json()
    assert run["ran"] and run["files_imported"] == 1
    assert run["new_transactions"] == 1
    assert run["dropped_rows"] >= 1
    # no filename / raw row content leaks to the browser
    rbody = str(run)
    assert "stmt.ofx" not in rbody and "SHELL" not in rbody and "NOTANUMBER" not in rbody


def test_dup_review_destructive_action_confirmed_and_safe(client):
    # UI safety (pairs with F1/S1): the destructive keep_one ("Remove duplicate")
    # must be gated behind an explicit confirm() that shows BOTH sides, while the
    # safe mark_distinct ("Not a duplicate") stays the easy default — so an
    # inclusive over-flag can never silently delete a real charge.
    text = client.get("/").text
    assert "Not a duplicate" in text and "mark_distinct" in text
    assert "Remove duplicate" in text and "keep_one" in text
    # keep_one is confirm()-gated; mark_distinct is not.
    assert "confirm(" in text
    assert "DELETE" in text  # the confirm spells out it is destructive
    # both amount·date sides are rendered for each pair (exSide ↔ inSide) and fed to
    # the confirm so the user can tell a real reformat-dup from two distinct charges.
    assert "exSide" in text and "inSide" in text
    assert "data-ex=" in text and "data-in=" in text
    # BOTH merchant strings (existing vs incoming) are rendered and fed to confirm —
    # the merchant names are the only signal distinguishing reformat-dup vs distinct.
    assert "existing_merchant" in text and "incoming_merchant" in text
    assert "data-exm=" in text and "data-inm=" in text


def test_fitid_collision_surfaced_with_correct_actions(client, tmp_path):
    # M2: a fitid_collision (same FITID, materially CHANGED amount) is a real
    # conflict that inserts nothing but needs a keep_one/accept_incoming decision.
    # It must be reachable via /api/conflicts AND rendered on the dashboard with its
    # correct actions ("Keep existing"=keep_one, "Use the changed values"=accept_incoming).
    importer.import_file(write_ofx(tmp_path / "c1.qfx", [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-10.00", "fitid": "FX1", "name": "SHELL"}]))
    importer.import_file(write_ofx(tmp_path / "c2.qfx", [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-99.00", "fitid": "FX1", "name": "SHELL"}]))
    rows = client.get("/api/conflicts").json()
    coll = [r for r in rows if r["kind"] == "fitid_collision"]
    assert len(coll) == 1                       # the changed-charge conflict is open
    # The dashboard JS surfaces collisions with their two real actions and a
    # confirm()-gated destructive accept_incoming.
    text = client.get("/").text
    assert "collision-bar" in text
    assert "renderCollisions" in text
    assert "fitid_collision" in text
    assert "Keep existing" in text and "keep_one" in text
    assert "Use the changed values" in text and "accept_incoming" in text
    # reconcile plumbing works end-to-end for a collision (accept_incoming adopts
    # the changed amount).
    rec = client.post("/api/reconcile",
                      json={"conflict_id": coll[0]["conflict_id"], "action": "accept_incoming"})
    assert rec.status_code == 200
    assert client.get("/api/conflicts").json() == []


def test_dup_dismiss_is_not_sticky_keyed_to_conflict_set(client):
    # M3: the dismissal is keyed to the CURRENT open-conflict id set, so NEW
    # possible duplicates re-show the banner instead of staying hidden.
    text = client.get("/").text
    assert "dupDismissKey" in text
    assert "dupKey(" in text
    assert "dupDismissed" not in text  # the old sticky boolean flag is gone


def test_report_endpoint(client):
    r = client.get("/api/report?month=2026-06")
    assert r.status_code == 200
    body = r.json()
    assert body["spend_total_cents"] == 5000
    assert body["income_cents"] == 200000


def test_report_no_pii_in_payload(client):
    # The browser must never receive raw account numbers / payee / memo.
    body = client.get("/api/report?month=2026-06").text
    assert "4111222233334444" not in body  # account number redacted before reaching the browser


def test_months_endpoint(client):
    assert "2026-06" in client.get("/api/months").json()


def test_set_and_list_limit(client):
    assert client.post("/api/limits", json={"category": "Groceries", "amount": "40"}).status_code == 200
    lims = client.get("/api/limits").json()
    assert any(x["category"] == "Groceries" and x["limit_cents"] == 4000 for x in lims)


def test_set_limit_rejects_bad_category(client):
    assert client.post("/api/limits", json={"category": "Nonsense", "amount": "40"}).status_code == 400


def test_set_limit_rejects_sub_cent(client):
    assert client.post("/api/limits", json={"category": "Groceries", "amount": "19.999"}).status_code == 400


def test_budget_posts_reject_malformed_json_with_400_not_500(client):
    # S-2: a malformed body must surface as HTTP 400, never an uncaught 500.
    bad = "not json{"
    for path in ("/api/limits", "/api/budgets/income", "/api/budgets/suggest",
                 "/api/subcategory-budget"):
        r = client.post(path, content=bad)
        assert r.status_code == 400, f"{path} returned {r.status_code} on malformed JSON"
    # Valid bodies still behave as before.
    assert client.post("/api/budgets/income", json={"amount": "5000"}).status_code == 200
    assert client.post("/api/budgets/suggest", json={}).status_code == 200


def test_budget_posts_reject_non_object_json_with_400_not_500(client, data_dir):
    # S-1: a JSON body that parses to a NON-object (bare number/string/array/null)
    # must surface as HTTP 400, never an uncaught 500 (the field access would raise
    # TypeError/AttributeError, which is outside the handlers' except tuple).
    # raise_server_exceptions=False makes a regression observable as 500 (not a raise).
    c = TestClient(server.create_app(), raise_server_exceptions=False, base_url="http://127.0.0.1")
    headers = {"content-type": "application/json"}
    for path in ("/api/limits", "/api/subcategory-budget", "/api/budgets/income"):
        for payload in ("5", "[]", '"x"', "null"):
            r = c.post(path, content=payload, headers=headers)
            assert r.status_code == 400, f"{path} on {payload!r} returned {r.status_code}"
    # Valid object bodies still behave exactly as before (200/400, never 500).
    assert c.post("/api/budgets/income", json={"amount": "5000"}).status_code == 200
    assert c.post("/api/limits", json={"category": "Groceries", "amount": "40"}).status_code == 200
    assert c.post("/api/limits", json={"category": "Groceries", "amount": "19.999"}).status_code == 400


def test_suggest_non_str_month_coerced_not_500(data_dir):
    # A non-string `month` (int/list) in the body must not reach reports' str ops
    # (month.startswith → AttributeError → uncaught 500). It is coerced to a string
    # at the endpoint boundary and returns a valid overview.
    db.init_schema()
    c = TestClient(server.create_app(), raise_server_exceptions=False, base_url="http://127.0.0.1")
    for payload in ({"month": 12345}, {"month": ["x"]}):
        r = c.post("/api/budgets/suggest", json=payload)
        assert r.status_code == 200, f"{payload!r} returned {r.status_code}"
        assert "overview" in r.json()
    # Valid string month still scopes; empty body still current month.
    assert c.post("/api/budgets/suggest", json={"month": "2026-01"}).status_code == 200
    assert c.post("/api/budgets/suggest", json={}).status_code == 200


def test_oversized_amount_returns_400_not_500(client, data_dir):
    # A >=29-digit amount overflows the default decimal context: quantize raises
    # decimal.InvalidOperation, which is NOT a ValueError. It must surface as a
    # clean 400 (AmountParseError -> ValueError), never an uncaught 500.
    # raise_server_exceptions=False makes a regression observable as 500 (not a raise).
    c = TestClient(server.create_app(), raise_server_exceptions=False, base_url="http://127.0.0.1")
    big = "9" * 29
    assert c.post("/api/limits", json={"category": "Groceries", "amount": big}).status_code == 400
    assert c.post("/api/budgets/income", json={"amount": big}).status_code == 400
    assert c.post("/api/subcategory-budget", json={
        "category": "Groceries", "subcategory": "Restaurants", "amount": big,
    }).status_code == 400


def test_budget_post_rejects_structural_category_with_400(client):
    # S-1: budgets are spend-only; a structural category is rejected, not 500.
    assert client.post("/api/limits", json={"category": "Transfer", "amount": "40"}).status_code == 400
    assert client.post("/api/limits", json={"category": "Income", "amount": "40"}).status_code == 400


def test_conflicts_endpoint_sanitized(client, tmp_path):
    # A near-dup conflict surfaced to the UI must carry no raw payee.
    importer.import_file(write_ofx(tmp_path / "stmt2.qfx", [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-50.00", "fitid": "G2",
         "name": "WALMART ACCT 4111222233334444"}]), detect_near_duplicates=True)
    rows = client.get("/api/conflicts").json()
    assert len(rows) == 1
    # Payload carries BOTH merchant strings (existing vs incoming) so the confirm
    # dialog can show what differs — plus the back-compat COALESCE `merchant`.
    assert set(rows[0].keys()) >= {"conflict_id", "kind", "merchant",
                                   "existing_merchant", "incoming_merchant"}
    assert "incoming_payee" not in rows[0]
    # Both merchant fields are sanitized merchant_norm only — no raw payee / account
    # number / filename leaks via either side.
    body = str(rows[0])
    assert "4111222233334444" not in body
    assert "incoming_payee" not in body and "raw_ofx" not in body and ".qfx" not in body


def test_reconcile_endpoint(client, tmp_path):
    importer.import_file(write_ofx(tmp_path / "stmt2.qfx", [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-50.00", "fitid": "G9", "name": "WALMART ACCT 4111222233334444"}]),
        detect_near_duplicates=True)
    cid = client.get("/api/conflicts").json()[0]["conflict_id"]
    r = client.post("/api/reconcile", json={"conflict_id": cid, "action": "keep_one"})
    assert r.status_code == 200
    assert client.get("/api/conflicts").json() == []


def test_auth_gate_when_token_set(data_dir, tmp_path, monkeypatch):
    db.init_schema()
    monkeypatch.setattr(server, "_API_TOKEN", "secret123")
    c = TestClient(server.create_app(), base_url="http://127.0.0.1")
    assert c.get("/api/months").status_code == 401
    assert c.get("/api/months", headers={"authorization": "Bearer secret123"}).status_code == 200


def test_non_loopback_without_token_refused(monkeypatch):
    monkeypatch.setattr(server, "_API_TOKEN", None)
    with pytest.raises(SystemExit):
        server.serve(host="0.0.0.0", port=8770)


# ── containerization Phase 1 (design 2026-06-13) ─────────────────────────────
def test_health_unauthenticated_and_db_free(data_dir, monkeypatch):
    # T1/I6: /health is 200 without a token (even when the gate is armed) and is
    # not under /api/, so it never needs auth and touches no DB.
    monkeypatch.setattr(server, "_API_TOKEN", "x" * 40)
    c = TestClient(server.create_app(), base_url="http://127.0.0.1")
    r = c.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    # an /api route IS gated — proving /health's openness is the route, not a missing gate
    assert c.get("/api/months").status_code == 401


def test_no_intake_blocks_raw_routes_keeps_dashboard_live(client, monkeypatch):
    # T7/I2: with LOCAL_BUDGET_NO_INTAKE armed, the three raw-file routes 403 while
    # a dashboard edit still works. Deny is by an explicit closed set, not method.
    monkeypatch.setattr(server, "_NO_INTAKE", True)
    for path in ("/api/upload", "/api/intake/run", "/api/intake/undo"):
        assert client.post(path).status_code == 403, f"{path} should be 403 under NO_INTAKE"
    # a non-intake dashboard write is unaffected
    assert client.post("/api/limits", json={"category": "Groceries", "amount": "40"}).status_code == 200
    # GET reads are unaffected
    assert client.get("/api/months").status_code == 200


def test_no_intake_403_is_distinct_from_401_auth(data_dir, monkeypatch):
    # The 401-vs-403 contract: missing token → 401 (re-auth); valid token but
    # raw-intake disabled → 403 (show error, don't re-auth).
    db.init_schema()
    monkeypatch.setattr(server, "_API_TOKEN", "y" * 40)
    monkeypatch.setattr(server, "_NO_INTAKE", True)
    c = TestClient(server.create_app(), base_url="http://127.0.0.1")
    assert c.post("/api/intake/run").status_code == 401                      # no token
    assert c.post("/api/intake/run",
                  headers={"authorization": "Bearer " + "y" * 40}).status_code == 403  # authed, but disabled


def test_serve_refuses_weak_token_on_non_loopback(monkeypatch):
    # T8/I3: a <32-char token must not gate full bank PII on the LAN.
    monkeypatch.setattr(server, "_API_TOKEN", "tooshort")
    with pytest.raises(SystemExit):
        server.serve(host="0.0.0.0", port=8770)


def test_frontend_sends_bearer_and_escapes_sinks(client):
    # T6: the browser attaches Authorization from localStorage on /api calls, and
    # the token is never baked into the served page (I7).
    text = client.get("/").text
    assert "localStorage" in text and "Authorization" in text and "Bearer" in text
    assert "window.fetch" in text                       # the auth wrapper is installed
    # T9/I8: the three merchant sinks render through esc()
    assert "${esc(exMerch)}" in text and "${esc(inMerch)}" in text
    assert "${esc(merch)}" in text
    assert "<td>${esc(m.merchant)}</td>" in text


def test_categories_endpoint(client):
    cats = client.get("/api/categories").json()
    assert "Shopping" in cats and "Health" in cats and "Subscriptions" in cats


def test_report_all_time(client, tmp_path):
    importer.import_file(write_ofx(tmp_path / "may.qfx",
        [{"trntype": "DEBIT", "dtposted": "20260515", "amount": "-100.00", "fitid": "M1", "name": "WALMART"}]))
    body = client.get("/api/report?month=all").json()
    assert body["month"] == "all"
    assert "trend" in body and len(body["trend"]) >= 1


def test_merchants_and_set_category(client, tmp_path):
    rows = client.get("/api/merchants").json()
    assert any(r["merchant"] == "WALMART ACCT 4111222233334444" or "WALMART" in r["merchant"] for r in rows)
    # set a category via the endpoint
    r = client.post("/api/merchant-category", json={"merchant": "WALMART", "category": "Shopping"})
    assert r.status_code == 200 and r.json()["updated"] >= 1


def test_add_category_endpoint(client):
    r = client.post("/api/add-category", json={"name": "Kid Activities"})
    assert r.status_code == 200
    assert "Kid Activities" in r.json()["categories"]
    assert "Kid Activities" in client.get("/api/categories").json()
    # invalid name (HTML metachar) → 400
    assert client.post("/api/add-category", json={"name": "<bad>"}).status_code == 400


def test_set_category_rejects_unknown(client):
    r = client.post("/api/merchant-category", json={"merchant": "WALMART", "category": "Bogus"})
    assert r.status_code == 400


def test_set_category_rejects_random_without_confirm(client):
    # F1 regression: the endpoint has no confirm_random passthrough, so posting
    # category="Random" (e.g. the review panel, if it ever offered it) must 400
    # rather than silently no-op. The frontend fix is to stop offering "Random"
    # in the review picker and to surface this 400 via postJSON's r.ok check.
    r = client.post("/api/merchant-category", json={"merchant": "WALMART", "category": "Random"})
    assert r.status_code == 400


def test_inbox_endpoint_counts_only(client, tmp_path, monkeypatch):
    from local_budget import db, inbox_adapter
    box = tmp_path / "inbox"
    box.mkdir()
    db.set_setting("inbox_dir", str(box))
    monkeypatch.setattr(inbox_adapter, "STABILITY_SECS", 0)
    (box / "stmt.csv").write_text('"06/03/2026","-5.00","*","","SHELL"\n')
    body = client.get("/api/inbox").json()
    assert body["new_files"] == 1
    # counts only — no filename / rows leak to the browser
    assert "filename" not in str(body) and "SHELL" not in str(body) and "stmt.csv" not in str(body)


def test_intake_run_and_undo_endpoints(client, tmp_path, monkeypatch):
    from local_budget import db, inbox_adapter
    box = tmp_path / "inbox"
    box.mkdir()
    db.set_setting("inbox_dir", str(box))
    monkeypatch.setattr(inbox_adapter, "STABILITY_SECS", 0)
    (box / "stmt.csv").write_text('"06/03/2026","-5.00","*","","SHELL"\n"06/04/2026","-9.00","*","","WALMART"\n')
    r = client.post("/api/intake/run").json()
    assert r["ran"] and r["files_imported"] == 1 and r["new_transactions"] == 2
    u = client.post("/api/intake/undo").json()
    assert u["undone"] and u["transactions_removed"] == 2


def test_intake_possible_duplicate_surfaced_and_reconcilable(client, tmp_path, monkeypatch):
    # F-1 (dashboard surface): two reformatted same-day/-amount CSVs in one drop
    # → /api/intake/run reports possible_duplicates>=1; /api/conflicts returns the
    # sanitized advisory row (no raw payee/filename); /api/reconcile keep_one
    # collapses the over-count in the spend total. No PII in any payload.
    from local_budget import db, inbox_adapter
    box = tmp_path / "inbox"
    box.mkdir()
    db.set_setting("inbox_dir", str(box))
    monkeypatch.setattr(inbox_adapter, "STABILITY_SECS", 0)
    # Same charge ($42, same day), reformatted text across two files → drift.
    (box / "a.csv").write_text('"06/03/2026","-42.00","*","","AMAZON MKTPL ABC 4111222233334444"\n')
    (box / "b.csv").write_text('"06/03/2026","-42.00","*","","AMAZON MKTPL XYZ STORE"\n')

    run = client.post("/api/intake/run").json()
    assert run["ran"] and run["files_imported"] == 2
    assert run["new_transactions"] == 2            # both posted (real spend counts)
    assert run["possible_duplicates"] >= 1         # surfaced, not silent
    # run_intake payload itself leaks no filename / raw row text.
    rbody = str(run)
    assert "a.csv" not in rbody and "b.csv" not in rbody
    assert "4111222233334444" not in rbody and "AMAZON MKTPL ABC" not in rbody

    conflicts = client.get("/api/conflicts").json()
    advisory = [c for c in conflicts if c["kind"] == "near_duplicate"]
    assert len(advisory) == 1
    c = advisory[0]
    # sanitized: merchant_norm only, never raw payee / account number / filename.
    cbody = str(c)
    assert "incoming_payee" not in c
    assert "4111222233334444" not in cbody          # account number redacted out
    assert "a.csv" not in cbody and "b.csv" not in cbody

    # Categorize both AMAZON variants so they enter the spend total (uncategorized
    # spend is excluded from spend_total_cents by design).
    for merch in ("AMAZON MKTPL ABC", "AMAZON MKTPL XYZ"):
        client.post("/api/merchant-category", json={"merchant": merch, "category": "Shopping"})

    # Both $42 AMAZON charges counted before resolve (plus the client fixture's
    # already-categorized WALMART -50); keep_one must remove exactly one $42.
    before = client.get("/api/report?month=2026-06").json()["spend_total_cents"]

    rec = client.post("/api/reconcile",
                      json={"conflict_id": c["conflict_id"], "action": "keep_one"})
    assert rec.status_code == 200
    after = client.get("/api/report?month=2026-06").json()["spend_total_cents"]
    assert before - after == 4200                   # over-count collapsed by one $42
    assert client.get("/api/conflicts").json() == []


# ── category drill-down endpoint (§2) ─────────────────────────────────────────
def test_transactions_endpoint_happy_and_empty(client):
    client.post("/api/merchant-category", json={"merchant": "WALMART", "category": "Shopping"})
    rows = client.get("/api/transactions?category=Shopping&month=all").json()
    assert rows and all(set(r) == {"merchant_norm", "amount_cents", "posted_date"} for r in rows)
    assert any("WALMART" in r["merchant_norm"] for r in rows)
    assert client.get("/api/transactions?category=Travel&month=all").json() == []   # spend cat, no rows


def test_transactions_endpoint_rejects_non_spend(client):
    # Allow-list (defense-in-depth): non-spend categories are never clickable bars.
    assert client.get("/api/transactions?category=Income&month=all").status_code == 400
    assert client.get("/api/transactions?category=Transfer&month=all").status_code == 400


def test_add_category_rejects_html_metachars(client):
    # CB-1: the one server-side choke point for the untrusted custom-category name.
    assert client.post("/api/add-category", json={"name": "Bad<script>"}).status_code == 400
    assert client.post("/api/add-category", json={"name": 'Quote"x'}).status_code == 400
    r = client.post("/api/add-category", json={"name": "Side Hustle"})        # clean name persists
    assert r.status_code == 200 and "Side Hustle" in r.json()["categories"]


def test_upload_stages_and_imports(client, tmp_path, monkeypatch):
    from local_budget import db, inbox_adapter
    box = tmp_path / "inbox"
    box.mkdir()
    db.set_setting("inbox_dir", str(box))
    monkeypatch.setattr(inbox_adapter, "STABILITY_SECS", 0)
    body = b'"06/03/2026","-9.00","*","","WALMART"\n'
    r = client.post("/api/upload?name=Checking.csv", content=body)
    assert r.status_code == 200 and r.json() == {"ok": True, "queued": 1}
    # the upload response leaks no filename (PII boundary) — counts only
    assert "Checking" not in str(r.json())
    run = client.post("/api/intake/run").json()
    assert run["ran"] and run["files_imported"] == 1 and run["new_transactions"] == 1


def test_upload_rejects_bad_extension(client, tmp_path):
    from local_budget import db
    box = tmp_path / "inbox"
    box.mkdir()
    db.set_setting("inbox_dir", str(box))
    assert client.post("/api/upload?name=evil.exe", content=b"x").status_code == 400
    assert client.post("/api/upload?name=note.txt", content=b"x").status_code == 400


def test_upload_rejects_oversized(client, tmp_path):
    from local_budget import db, inbox_adapter
    box = tmp_path / "inbox"
    box.mkdir()
    db.set_setting("inbox_dir", str(box))
    big = b"x" * (inbox_adapter.MAX_FILE_BYTES + 1)
    assert client.post("/api/upload?name=big.csv", content=big).status_code == 413


def test_upload_filename_confined_to_basename(client, tmp_path, monkeypatch):
    # A traversal-laden upload name is reduced to its basename and confined to the
    # inbox — nothing is written outside the drop folder.
    from local_budget import db, inbox_adapter
    box = tmp_path / "inbox"
    box.mkdir()
    db.set_setting("inbox_dir", str(box))
    monkeypatch.setattr(inbox_adapter, "STABILITY_SECS", 0)
    target = tmp_path / "pwned.csv"
    r = client.post("/api/upload?name=" + "..%2F..%2Fpwned.csv", content=b'"06/03/2026","-1.00","*","","X"\n')
    assert r.status_code == 200
    assert not target.exists()                       # did NOT escape the inbox
    assert (box / "pwned.csv").exists()              # staged as a basename inside the inbox


def test_inbox_reports_last_import_at(client):
    body = client.get("/api/inbox").json()
    assert "last_import_at" in body and body["last_import_at"]   # the fixture imported an OFX


# ── Budgets tab endpoints (design 2026-06-12) ────────────────────────────────
def test_budgets_overview_shape_and_no_pii(client):
    from local_budget import budgets
    budgets.set_limit("Shopping", 60000)
    body = client.get("/api/budgets").json()
    assert {"month", "factor", "expected_income_cents", "actual_income_cents",
            "total_budgeted_cents", "to_allocate_cents", "categories"} <= set(body)
    assert body["factor"] == 1   # default scope is the current month
    shop = next(c for c in body["categories"] if c["category"] == "Shopping")
    assert shop["monthly_budget_cents"] == 60000 and shop["budget_cents"] == 60000   # factor 1 → equal
    # categories + integer amounts only — no merchant/payee/account/filename
    s = str(body)
    assert "4111222233334444" not in s and "payee" not in s.lower() and "WALMART ACCT" not in s


def test_budgets_income_set_clear_and_validate(client):
    assert client.post("/api/budgets/income", json={"amount": "4200"}).status_code == 200
    assert client.get("/api/budgets").json()["expected_income_cents"] == 420000
    assert client.post("/api/budgets/income", json={"amount": ""}).status_code == 200   # clear
    assert client.get("/api/budgets").json()["expected_income_cents"] == 0
    assert client.post("/api/budgets/income", json={"amount": "-5"}).status_code == 400
    assert client.post("/api/budgets/income", json={"amount": "10.999"}).status_code == 400


def test_budgets_limit_set_and_clear_via_limits_endpoint(client):
    assert client.post("/api/limits", json={"category": "Shopping", "amount": "500"}).status_code == 200
    cats = {c["category"]: c["budget_cents"] for c in client.get("/api/budgets").json()["categories"]}
    assert cats.get("Shopping") == 50000
    assert client.post("/api/limits", json={"category": "Shopping", "amount": ""}).status_code == 200   # clear
    cats = {c["category"]: c["budget_cents"] for c in client.get("/api/budgets").json()["categories"]}
    assert cats.get("Shopping") is None


def test_budgets_suggest_fills_only_empties(client):
    from local_budget import budgets
    # The fixture imported a -$50 WALMART (Shopping-ish) + payroll; give one category a
    # user value and confirm suggest never overwrites it.
    budgets.set_limit("Groceries", 12345)
    r = client.post("/api/budgets/suggest", json={}).json()
    assert "set" in r and "overview" in r
    cats = {c["category"]: c["budget_cents"] for c in r["overview"]["categories"]}
    assert cats.get("Groceries") == 12345   # preserved


# ── remove category (global vocabulary, merge) ───────────────────────────────
def test_remove_category_endpoint_merges_and_validates(client):
    client.post("/api/add-category", json={"name": "Pets"})
    r = client.post("/api/remove-category", json={"name": "Pets", "merge_into": "Shopping"})
    assert r.status_code == 200
    body = r.json()
    assert "Pets" not in body["categories"] and "Pets" not in client.get("/api/categories").json()
    assert {"moved_txns", "moved_rules", "merged_budget", "summed_limit_cents"} <= set(body)
    # no PII leaks in the payload
    s = str(body)
    assert "4111222233334444" not in s and "payee" not in s.lower()
    # validation 400s: protected source, protected target, self-merge, unknown
    assert client.post("/api/remove-category", json={"name": "Income", "merge_into": "Shopping"}).status_code == 400
    assert client.post("/api/remove-category", json={"name": "Shopping", "merge_into": "Random"}).status_code == 400
    assert client.post("/api/remove-category", json={"name": "Shopping", "merge_into": "Shopping"}).status_code == 400
    assert client.post("/api/remove-category", json={"name": "Nope", "merge_into": "Shopping"}).status_code == 400


# ── income sources ("Where your money comes from") endpoints ─────────────────
def test_income_sources_and_drill(client):
    from local_budget import db
    with db.connect() as conn:   # make the ACME PAYROLL credit a real income row
        conn.execute("UPDATE transactions SET category='Income' WHERE amount_cents > 0")
        conn.commit()
    rows = client.get("/api/income-sources?month=all").json()
    assert rows and any(r["source"] == "Acme" for r in rows)
    assert all(set(r) >= {"source", "total_cents", "count", "other"} for r in rows)
    # drill the Acme source — sanitized columns only
    txns = client.get("/api/income-transactions?source=Acme&month=all").json()
    assert txns and all(set(t) == {"merchant_norm", "amount_cents", "posted_date"} for t in txns)


def test_income_transactions_unmatched_returns_empty_not_400(client):
    # No allow-list: a bogus source returns [] (200), never a 400.
    r = client.get("/api/income-transactions?source=Nonexistent&month=all")
    assert r.status_code == 200 and r.json() == []


def test_budgets_overview_exposes_suggested_income(client):
    # The income "use 3-mo avg" button is fed by suggested_income_cents (avg salary
    # over the last 3 FULL months). Field must be present + integer cents, no PII.
    body = client.get("/api/budgets").json()
    assert "suggested_income_cents" in body and isinstance(body["suggested_income_cents"], int)


def test_normalize_endpoints_and_no_pii(client):
    # Seed two Anthropic spellings, normalize via the endpoint (deterministic
    # built-in brand aliases — no AI), confirm collapse + no PII.
    from local_budget import db
    with db.connect() as conn:
        aid = conn.execute("SELECT account_id FROM accounts LIMIT 1").fetchone()[0]
        for i, m in enumerate(["ANTHROPIC CLAUDE", "PURCHASE ANTHROPIC"]):
            conn.execute(
                "INSERT INTO transactions (account_id,fitid,posted_date,amount_cents,status,txn_type,"
                "payee,memo,merchant_norm,category,subcategory,category_source,raw_ofx,imported_at,import_run_id) "
                "VALUES (?,?,?,?,'posted','M','M','M',?,'Subscriptions',NULL,'x','',?,1)",
                (aid, f"an{i}", "2026-05-10", -2000, m, "2026-06-01"))
    r = client.post("/api/normalize").json()
    assert "txns_updated" in r and "budgets_merged" in r and r["txns_updated"] >= 1
    body = client.get("/api/budgets?month=all").text  # canonical visible; no raw payee/account leaks
    assert "4111222233334444" not in body
    # the two Anthropic spellings collapsed to ONE Subscriptions subcategory
    subs = [c for c in client.get("/api/budgets?month=all").json()["categories"]
            if c["category"] == "Subscriptions"][0]["subcategories"]
    names = {s["subcategory"] for s in subs}
    assert names == {"Anthropic"}
    # undo works
    assert client.post("/api/normalize/undo").json()["undone"] is True


def test_normalize_confirm_validates(client):
    assert client.post("/api/normalize/confirm", json={"canonical": ""}).status_code == 400
    assert client.post("/api/normalize/confirm", json=[1, 2]).status_code == 400
    # FIX 1: non-string members -> 400 (not 500); members as a string -> 400 (not
    # silently split into ['A','B','C']); valid members still works.
    assert client.post("/api/normalize/confirm",
                       json={"canonical": "X", "members": [1, 2, 3]}).status_code == 400
    assert client.post("/api/normalize/confirm",
                       json={"canonical": "X", "members": "ABC"}).status_code == 400
    assert client.post("/api/normalize/confirm",
                       json={"canonical": "WidgetCo", "members": ["WIDGETCO ONE"]}).status_code == 200


def test_normalize_confirm_malformed_body_is_400_not_500(data_dir):
    # FIX 1: request.json() raising on a non-JSON / empty body must surface as HTTP
    # 400, never an uncaught 500. raise_server_exceptions=False makes a regression
    # observable as a 500 status rather than a raised exception.
    c = TestClient(server.create_app(), raise_server_exceptions=False, base_url="http://127.0.0.1")
    headers = {"content-type": "application/json"}
    assert c.post("/api/normalize/confirm", content=b"not json",
                  headers=headers).status_code == 400
    assert c.post("/api/normalize/confirm", content=b"",
                  headers=headers).status_code == 400


def test_normalize_confirm_builtin_token_does_not_break_collapse(client):
    # FIX 4: confirming a bare built-in token must return 200 and NOT degrade the
    # built-in's broad token-anywhere collapse for that vendor.
    from local_budget import db, merchants, normalize
    with db.connect() as conn:
        merchants.seed_builtin_aliases(conn)
        aid = conn.execute("SELECT account_id FROM accounts LIMIT 1").fetchone()[0]
        for i, m in enumerate(["PURCHASE ANTHROPIC", "ANTHROPIC CLAUDE"]):
            conn.execute(
                "INSERT INTO transactions (account_id,fitid,posted_date,amount_cents,status,txn_type,"
                "payee,memo,merchant_norm,category,subcategory,category_source,raw_ofx,imported_at,import_run_id) "
                "VALUES (?,?,?,?,'posted','M','M','M',?,'Subscriptions',NULL,'x','',?,1)",
                (aid, f"ab{i}", "2026-05-10", -2000, m, "2026-06-01"))
    r = client.post("/api/normalize/confirm",
                    json={"canonical": "Something Else", "members": ["ANTHROPIC"]})
    assert r.status_code == 200
    # built-in still collapses both spellings to one Subscriptions sub.
    normalize.apply_aliases()
    subs = [c for c in client.get("/api/budgets?month=all").json()["categories"]
            if c["category"] == "Subscriptions"][0]["subcategories"]
    assert {s["subcategory"] for s in subs} == {"Anthropic"}


@pytest.mark.parametrize("bad_body", [
    {"canonical": None, "members": ["WIDGETCO ONE"]},        # null canonical (coercion trap)
    {"canonical": {"x": 1}, "members": ["WIDGETCO ONE"]},    # dict canonical
    {"canonical": 123, "members": ["WIDGETCO ONE"]},         # int canonical
    {"canonical": "   ", "members": ["WIDGETCO ONE"]},       # whitespace-only canonical
    {"members": ["WIDGETCO ONE"]},                           # missing canonical
    {"canonical": "X", "members": "ABC"},                    # members not a list
    {"canonical": "X", "members": [1, 2, 3]},                # members non-string elements
])
def test_normalize_confirm_hostile_input_is_400_never_500_or_corruption(data_dir, tmp_path, bad_body):
    # FIX 6: the WHOLE hostile-input class on /api/normalize/confirm must be 400 —
    # never 500, never a silent str()-coercion that persists a garbage
    # canonical_merchant. raise_server_exceptions=False makes a regression to a 500
    # observable as a status, not a raised exception.
    db.init_schema()
    importer.import_file(write_ofx(tmp_path / "stmt.qfx", [
        {"trntype": "DEBIT", "dtposted": "20260510", "amount": "-20.00", "fitid": "WC0",
         "name": "WIDGETCO ONE"},
    ]))
    # A real Subscriptions txn proves the valid path stays 200 (and is the corruption target).
    with db.connect() as conn:
        conn.execute("UPDATE transactions SET category='Subscriptions' WHERE merchant_norm LIKE '%WIDGETCO%'")
    c = TestClient(server.create_app(), raise_server_exceptions=False, base_url="http://127.0.0.1")
    assert c.post("/api/normalize/confirm", json=bad_body).status_code == 400
    # The null-canonical coercion trap must NOT have persisted a "None" canonical_merchant.
    with db.connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE canonical_merchant = 'None'").fetchone()[0]
    assert n == 0
    # A valid body still works (200).
    assert c.post("/api/normalize/confirm",
                  json={"canonical": "WidgetCo", "members": ["WIDGETCO ONE"]}).status_code == 200


# ── timeframe drives all tabs: scope-following GET endpoints validate `month` ──
@pytest.mark.parametrize("bad", ["lastABC", "last0", "2026-13", "2026-5", "garbage", "all\n"])
@pytest.mark.parametrize("path", ["/api/report", "/api/insights", "/api/budgets", "/api/income-sources"])
def test_scope_following_endpoints_reject_malformed_month_400(client, path, bad):
    # A malformed timeframe scope is a client error (400), never a 500.
    assert client.get(path, params={"month": bad}).status_code == 400


@pytest.mark.parametrize("path,month", [
    ("/api/report", "2026-06"), ("/api/insights", "last3"),
    ("/api/budgets", "all"), ("/api/income-sources", "2026-06"),
])
def test_scope_following_endpoints_accept_valid_month_200(client, path, month):
    assert client.get(path, params={"month": month}).status_code == 200


# ── budget setup wizard (zero-based first-run; design 2026-06-12) ──────────────
def test_budget_setup_sets_income_and_limits_atomically_and_flips_onboarded(client):
    assert client.get("/api/budgets").json()["onboarded"] is False     # fresh
    r = client.post("/api/budgets/setup",
                    json={"income": "5,000", "limits": {"Groceries": "500", "Dining Out": "200"}})
    assert r.status_code == 200
    ov = r.json()["overview"]
    assert ov["onboarded"] is True
    assert ov["expected_income_cents"] == 500000
    assert ov["to_allocate_cents"] == 500000 - 50000 - 20000     # income − the two limits
    lim = {c["category"]: c["budget_cents"] for c in ov["categories"]}
    assert lim["Groceries"] == 50000 and lim["Dining Out"] == 20000


def test_budget_setup_blank_amount_clears_that_envelope(client):
    client.post("/api/budgets/setup", json={"income": "5000", "limits": {"Groceries": "500"}})
    ov = client.post("/api/budgets/setup",
                     json={"income": "5000", "limits": {"Groceries": ""}}).json()["overview"]
    g = [c for c in ov["categories"] if c["category"] == "Groceries"][0]
    assert g["budget_cents"] is None                              # blank cleared it


@pytest.mark.parametrize("bad_body", [
    {"income": "5000", "limits": {"Groceries": "1e9"}},          # scientific-notation amount
    {"income": "5000", "limits": {"Groceries": "abc"}},          # unparseable amount
    {"income": "5000", "limits": {"Groceries": "-50"}},          # non-positive limit
    {"income": "5000", "limits": {"Transfer": "500"}},           # structural category
    {"income": "5000", "limits": {"Nonexistent": "500"}},        # unknown category
    {"income": "not-a-number", "limits": {}},                    # bad income (unparseable)
    {"income": "0", "limits": {"Groceries": "500"}},             # zero income — must 400, not 500
    {"income": "0.00", "limits": {"Groceries": "500"}},          # zero income (decimal form)
    {"income": "-50", "limits": {"Groceries": "500"}},           # negative income
    {"income": "", "limits": {"Groceries": "500"}},              # blank income — setup requires income
    {"limits": {"Groceries": "500"}},                            # missing income key entirely
    {"income": "5000", "limits": "notanobject"},                 # limits not an object
    "notanobject",                                               # body not an object
])
def test_budget_setup_hostile_input_is_400_and_persists_nothing(client, bad_body):
    # Validation happens BEFORE any write, so a bad body 400s with nothing applied
    # (never a 500, never a half-saved budget).
    assert client.get("/api/budgets").json()["onboarded"] is False
    assert client.post("/api/budgets/setup", json=bad_body).status_code == 400
    ov = client.get("/api/budgets").json()
    assert ov["onboarded"] is False and ov["expected_income_cents"] == 0
    assert ov["total_budgeted_cents"] == 0


def test_budget_onboarded_endpoint_flips_flag_without_touching_budget(client):
    assert client.get("/api/budgets").json()["onboarded"] is False
    assert client.post("/api/budgets/onboarded").status_code == 200
    ov = client.get("/api/budgets").json()
    assert ov["onboarded"] is True
    assert ov["expected_income_cents"] == 0 and ov["total_budgeted_cents"] == 0   # skip ≠ save


# ── CSRF boundary (siege S2): mutating /api/* rejects cross-origin requests ────
# Loopback binding doesn't stop a browser the user drives from firing no-cors
# POSTs at 127.0.0.1:8770; several mutating routes take no body, so no preflight
# fires. The middleware requires a same-host/loopback Origin (or, header-less
# clients, a loopback Host) whenever no bearer token is configured.
def test_csrf_cross_origin_post_rejected(client):
    r = client.post("/api/intake/undo", headers={"origin": "http://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/budgets/onboarded", headers={"origin": "https://evil.example:8770"})
    assert r.status_code == 403
    # "null" opaque origin (sandboxed iframe) is also rejected
    assert client.post("/api/intake/undo", headers={"origin": "null"}).status_code == 403


def test_csrf_same_origin_and_loopback_allowed(client):
    # same-origin dashboard fetch: Origin matches Host
    r = client.post("/api/budgets/onboarded", headers={"origin": "http://127.0.0.1"})
    assert r.status_code == 200
    # another loopback-served page (local tooling) is trusted
    r = client.post("/api/budgets/income", json={"amount": "5000"},
                    headers={"origin": "http://localhost:9999"})
    assert r.status_code == 200


def test_csrf_no_origin_requires_loopback_host(data_dir):
    # curl-style clients (no Origin): loopback Host passes, non-loopback 403s
    # (DNS-rebinding guard). GETs are never CSRF-gated.
    db.init_schema()
    c = TestClient(server.create_app(), base_url="http://127.0.0.1")
    assert c.post("/api/budgets/onboarded").status_code == 200
    evil = TestClient(server.create_app(), raise_server_exceptions=False,
                      base_url="http://rebound.evil.example")
    assert evil.post("/api/budgets/onboarded").status_code == 403
    assert evil.get("/api/budgets").status_code == 200


def test_csrf_skipped_when_token_configured(data_dir, monkeypatch):
    # Token deployments: the bearer gate is already CSRF-proof (browsers cannot
    # attach the header cross-origin) — the Origin check must not double-gate.
    db.init_schema()
    monkeypatch.setattr(server, "_API_TOKEN", "T" * 43)
    c = TestClient(server.create_app(), base_url="http://192.168.1.5")
    r = c.post("/api/budgets/onboarded",
               headers={"authorization": "Bearer " + "T" * 43,
                        "origin": "http://evil.example"})
    assert r.status_code == 200
    # and without the token it is still a 401 (auth outranks CSRF)
    assert c.post("/api/budgets/onboarded").status_code == 401
