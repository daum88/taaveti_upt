"""SQLite persistence for immutable filing documents and their derived briefs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from adapters.sqlite.connection import get_db


class FilingBriefsStore:
    """Hide document, brief, and per-ticker scan-status bookkeeping."""

    def is_scan_fresh(self, ticker: str, fetched_after: str) -> bool:
        with get_db() as conn:
            row = conn.execute("SELECT fetched_at FROM filing_scan_status WHERE ticker=?", (ticker,)).fetchone()
        return bool(row and row["fetched_at"] and row["fetched_at"] >= fetched_after)

    def record_scan(self, ticker: str, fetched_at: str, status: str, filing_count: int) -> None:
        with get_db() as conn:
            conn.execute(
                """INSERT INTO filing_scan_status (ticker, fetched_at, status, filing_count)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(ticker) DO UPDATE SET fetched_at=excluded.fetched_at,
                       status=excluded.status, filing_count=excluded.filing_count""",
                (ticker, fetched_at, status, filing_count),
            )

    def persist_document(self, document: Mapping[str, Any], fetched_at: str) -> bool:
        """Insert one immutable document; returns whether it was newly stored."""
        with get_db() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO filing_documents
                   (accession, ticker, form, filed_at, doc_url, excerpt, content_hash, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    document["accession"],
                    document["ticker"],
                    document["form"],
                    document["filed_at"],
                    document["doc_url"],
                    document["excerpt"],
                    document["content_hash"],
                    fetched_at,
                ),
            )
        return cursor.rowcount > 0

    def document(self, accession: str) -> dict[str, Any] | None:
        with get_db() as conn:
            row = conn.execute(
                "SELECT accession, ticker, form, filed_at, doc_url, excerpt, content_hash FROM filing_documents WHERE accession=?",
                (accession,),
            ).fetchone()
        return dict(row) if row else None

    def brief_for_accession(self, accession: str) -> dict[str, Any] | None:
        with get_db() as conn:
            row = conn.execute(
                "SELECT accession, ticker, summarized_at, model_name, status, brief_json FROM filing_briefs WHERE accession=?",
                (accession,),
            ).fetchone()
        return dict(row) if row else None

    def brief_for_hash(self, content_hash: str) -> dict[str, Any] | None:
        """Return any already-derived brief for identical document content."""
        with get_db() as conn:
            row = conn.execute(
                """SELECT b.accession, b.ticker, b.summarized_at, b.model_name, b.status, b.brief_json
                   FROM filing_briefs b
                   JOIN filing_documents d ON d.accession = b.accession
                   WHERE d.content_hash = ?
                   ORDER BY b.summarized_at DESC LIMIT 1""",
                (content_hash,),
            ).fetchone()
        return dict(row) if row else None

    def persist_brief(
        self, accession: str, ticker: str, summarized_at: str, model_name: str, status: str, brief_json: str
    ) -> None:
        with get_db() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO filing_briefs
                   (accession, ticker, summarized_at, model_name, status, brief_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (accession, ticker, summarized_at, model_name, status, brief_json),
            )

    def briefs(self, ticker: str, *, filed_before: str, since: str, limit: int) -> list[dict[str, Any]]:
        """Return briefs observable at ``filed_before`` (ISO timestamp), newest first."""
        with get_db() as conn:
            rows = conn.execute(
                """SELECT d.accession, d.form, d.filed_at, d.doc_url, b.status, b.brief_json
                   FROM filing_documents d
                   JOIN filing_briefs b ON b.accession = d.accession
                   WHERE d.ticker=? AND d.filed_at <= ? AND d.filed_at >= ?
                   ORDER BY d.filed_at DESC LIMIT ?""",
                (ticker, filed_before, since, limit),
            ).fetchall()
        return [dict(row) for row in rows]
