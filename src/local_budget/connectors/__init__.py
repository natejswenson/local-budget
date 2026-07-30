"""Connectors — authenticated pulls from outside sources that ENRICH the ledger.

A connector never invents or edits a bank transaction. It fetches facts from
somewhere else (an Amazon account, eventually others), stores them in its own
tables, and reconciles them against `transactions` by amount and date. The
ledger stays the record of what the bank said; a connector only explains it.
"""
