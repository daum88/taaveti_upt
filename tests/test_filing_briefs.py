"""Filed-report briefs coverage: background refresh pipeline + read-only PIT reads."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

from adapters.edgar.errors import EdgarSourceError
from adapters.news_data.errors import NewsSourceError
from adapters.sqlite.connection import close_db, get_db, init_db
from services import filing_briefs


def _init(tmp_path, monkeypatch):
    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()


def _filing(form="10-Q", accession="0000320193-26-000091", published="2026-07-31T16:31:22+00:00"):
    return {
        "form": form,
        "link": "https://www.sec.gov/Archives/edgar/data/320193/000032019326000091/doc.htm",
        "published_at": published,
        "accession": accession,
        "primary_document": "doc.htm",
    }


def _document(filing, excerpt="Management discusses revenue growth and margin expansion in detail."):
    return {
        "accession": filing["accession"],
        "form": filing["form"],
        "filed_at": filing["published_at"],
        "doc_url": filing["link"],
        "excerpt": excerpt,
        "content_hash": hashlib.sha256(excerpt.encode()).hexdigest(),
    }


def _ok_summary(*_args):
    return json.dumps(
        {
            "status": "ok",
            "guidance": "raised",
            "key_points": ["Revenue grew 10%", "Services margin expanded"],
            "risks": ["Supply constraints"],
            "outlook": "Next quarter revenue guided higher",
            "capital_allocation": "New $10B buyback",
            "tone": "positive",
        }
    )


def _listing_fetcher(filings_by_ticker, calls):
    def fetch(ticker, lookback_hours, *, settings):
        calls.append((ticker, lookback_hours))
        return list(filings_by_ticker.get(ticker.upper(), ()))

    return fetch


def _excerpt_fetcher(documents, calls):
    def fetch(ticker, filing, *, settings):
        calls.append(filing["accession"])
        return documents.get(filing["accession"])

    return fetch


def test_refresh_summarizes_and_persists_new_filings(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    listing_calls, excerpt_calls = [], []
    filing = _filing()

    counts = filing_briefs.refresh(
        ["aapl"],
        listing_fetcher=_listing_fetcher({"AAPL": [filing]}, listing_calls),
        excerpt_fetcher=_excerpt_fetcher({filing["accession"]: _document(filing)}, excerpt_calls),
        caller=_ok_summary,
    )

    assert listing_calls == [("AAPL", 2400)]
    assert excerpt_calls == [filing["accession"]]
    assert counts == {"scanned": 1, "cached": 0, "empty": 0, "failed": 0, "new_documents": 1}

    result = filing_briefs.briefs(["AAPL"], as_of=datetime(2026, 8, 4, tzinfo=UTC))
    entry = result["AAPL"][0]
    assert entry["form"] == "10-Q"
    assert entry["status"] == "ok"
    assert entry["brief"]["guidance"] == "raised"
    assert entry["brief"]["key_points"] == ["Revenue grew 10%", "Services margin expanded"]
    assert entry["brief"]["outlook"] == "Next quarter revenue guided higher"
    assert entry["brief"]["capital_allocation"] == "New $10B buyback"
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM filing_documents").fetchone()[0] == 1
        brief = conn.execute("SELECT * FROM filing_briefs").fetchone()
    assert brief["status"] == "ok"
    assert brief["model_name"] == "injected"
    close_db()


def test_refresh_fetches_once_ever_and_summarizes_once_per_filing(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    listing_calls, excerpt_calls, llm_calls = [], [], []
    filing = _filing()
    fetcher = _listing_fetcher({"AAPL": [filing]}, listing_calls)
    extractor = _excerpt_fetcher({filing["accession"]: _document(filing)}, excerpt_calls)

    def caller(system, user):
        llm_calls.append((system, user))
        return _ok_summary()

    filing_briefs.refresh(["AAPL"], listing_fetcher=fetcher, excerpt_fetcher=extractor, caller=caller)
    with get_db() as conn:
        conn.execute(
            "UPDATE filing_scan_status SET fetched_at=?", ((datetime.now(UTC) - timedelta(days=1)).isoformat(),)
        )
    filing_briefs.refresh(["AAPL"], listing_fetcher=fetcher, excerpt_fetcher=extractor, caller=caller)

    assert len(listing_calls) == 2  # stale scan re-lists
    assert excerpt_calls == [filing["accession"]]  # document is immutable, never refetched
    assert len(llm_calls) == 1  # summarized exactly once
    close_db()


def test_refresh_heals_a_document_left_unbriefed_by_an_interrupted_run(tmp_path, monkeypatch):
    """A crash between document persist and summary must not orphan the filing forever."""
    _init(tmp_path, monkeypatch)
    filing = _filing()
    document = _document(filing)
    now = datetime.now(UTC).isoformat()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO filing_documents (accession, ticker, form, filed_at, doc_url, excerpt, content_hash, fetched_at)
               VALUES (?, 'AAPL', ?, ?, ?, ?, ?, ?)""",
            (
                document["accession"],
                document["form"],
                document["filed_at"],
                document["doc_url"],
                document["excerpt"],
                document["content_hash"],
                now,
            ),
        )
        conn.execute("INSERT INTO filing_scan_status (ticker, fetched_at, status) VALUES ('AAPL', '2000-01-01', 'ok')")
    excerpt_calls, llm_calls = [], []

    def caller(system, user):
        llm_calls.append(user)
        return _ok_summary()

    counts = filing_briefs.refresh(
        ["AAPL"],
        listing_fetcher=_listing_fetcher({"AAPL": [filing]}, []),
        excerpt_fetcher=_excerpt_fetcher({}, excerpt_calls),
        caller=caller,
    )

    assert excerpt_calls == []  # stored excerpt is reused; nothing is refetched
    assert len(llm_calls) == 1
    assert counts["new_documents"] == 0
    result = filing_briefs.briefs(["AAPL"], as_of=datetime(2026, 8, 4, tzinfo=UTC))
    assert result["AAPL"][0]["brief"]["guidance"] == "raised"
    close_db()


def test_refresh_reuses_the_summary_for_identical_content(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    llm_calls = []
    original = _filing(accession="0000320193-26-000091")
    amendment = _filing(form="10-Q/A", accession="0000320193-26-000099", published="2026-08-02T09:00:00+00:00")
    documents = {
        original["accession"]: _document(original),
        amendment["accession"]: _document(amendment),  # identical excerpt => identical content hash
    }

    def caller(system, user):
        llm_calls.append(user)
        return _ok_summary()

    filing_briefs.refresh(
        ["AAPL"],
        listing_fetcher=_listing_fetcher({"AAPL": [original, amendment]}, []),
        excerpt_fetcher=_excerpt_fetcher(documents, []),
        caller=caller,
    )

    assert len(llm_calls) == 1
    result = filing_briefs.briefs(["AAPL"], as_of=datetime(2026, 8, 4, tzinfo=UTC))
    assert [entry["form"] for entry in result["AAPL"]] == ["10-Q/A", "10-Q"]
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM filing_briefs").fetchone()[0] == 2
    close_db()


def test_refresh_isolates_ticker_failures(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    filing = _filing()

    def listings(ticker, _lookback, *, settings):
        if ticker == "MSFT":
            raise NewsSourceError("submissions down")
        return [filing]

    counts = filing_briefs.refresh(
        ["MSFT", "AAPL"],
        listing_fetcher=listings,
        excerpt_fetcher=_excerpt_fetcher({filing["accession"]: _document(filing)}, []),
        caller=_ok_summary,
    )

    assert counts["failed"] == 1
    assert counts["new_documents"] == 1
    result = filing_briefs.briefs(["MSFT", "AAPL"], as_of=datetime(2026, 8, 4, tzinfo=UTC))
    assert "MSFT" not in result
    assert result["AAPL"][0]["brief"]["guidance"] == "raised"
    with get_db() as conn:
        assert conn.execute("SELECT status FROM filing_scan_status WHERE ticker='MSFT'").fetchone()[0] == "failed"
    close_db()


def test_refresh_isolates_single_filing_extraction_failures(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    broken = _filing(accession="0000320193-26-000090", published="2026-07-15T10:00:00+00:00")
    working = _filing(accession="0000320193-26-000091")

    def extractor(ticker, filing, *, settings):
        if filing["accession"] == broken["accession"]:
            raise EdgarSourceError("document vanished")
        return _document(filing)

    counts = filing_briefs.refresh(
        ["AAPL"],
        listing_fetcher=_listing_fetcher({"AAPL": [broken, working]}, []),
        excerpt_fetcher=extractor,
        caller=_ok_summary,
    )

    assert counts["failed"] == 1
    assert counts["new_documents"] == 1
    result = filing_briefs.briefs(["AAPL"], as_of=datetime(2026, 8, 4, tzinfo=UTC))
    assert [entry["accession"] for entry in result["AAPL"]] == [working["accession"]]
    with get_db() as conn:
        assert conn.execute("SELECT status FROM filing_scan_status WHERE ticker='AAPL'").fetchone()[0] == "failed"
    close_db()


def test_failed_scan_is_retried_on_the_next_refresh(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    filing = _filing()
    listing_calls = []

    def extractor(ticker, filing, *, settings):
        raise EdgarSourceError("rate limited")

    first = filing_briefs.refresh(
        ["AAPL"],
        listing_fetcher=_listing_fetcher({"AAPL": [filing]}, listing_calls),
        excerpt_fetcher=extractor,
        caller=_ok_summary,
    )
    assert first["failed"] == 1

    second = filing_briefs.refresh(
        ["AAPL"],
        listing_fetcher=_listing_fetcher({"AAPL": [filing]}, listing_calls),
        excerpt_fetcher=_excerpt_fetcher({filing["accession"]: _document(filing)}, []),
        caller=_ok_summary,
    )

    assert len(listing_calls) == 2  # failed scan was not treated as fresh
    assert second == {"scanned": 1, "cached": 0, "empty": 0, "failed": 0, "new_documents": 1}
    close_db()


def test_briefs_read_excludes_filings_filed_after_as_of(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    older = _filing(accession="0000320193-26-000050", published="2026-05-01T10:00:00+00:00")
    newer = _filing(accession="0000320193-26-000091", published="2026-07-31T16:31:22+00:00")
    documents = {filing["accession"]: _document(filing) for filing in (older, newer)}

    filing_briefs.refresh(
        ["AAPL"],
        listing_fetcher=_listing_fetcher({"AAPL": [older, newer]}, []),
        excerpt_fetcher=_excerpt_fetcher(documents, []),
        caller=_ok_summary,
    )

    result = filing_briefs.briefs(["AAPL"], as_of=datetime(2026, 6, 1, tzinfo=UTC))

    assert [entry["accession"] for entry in result["AAPL"]] == [older["accession"]]
    close_db()


def test_validation_failure_keeps_a_metadata_only_brief(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    filing = _filing()

    filing_briefs.refresh(
        ["AAPL"],
        listing_fetcher=_listing_fetcher({"AAPL": [filing]}, []),
        excerpt_fetcher=_excerpt_fetcher({filing["accession"]: _document(filing)}, []),
        caller=lambda *_: "not json at all",
    )

    result = filing_briefs.briefs(["AAPL"], as_of=datetime(2026, 8, 4, tzinfo=UTC))
    entry = result["AAPL"][0]
    assert entry["status"] == "metadata_only"
    assert entry["brief"] == {"status": "metadata_only"}
    lines = filing_briefs.prompt_lines(result)
    assert any("metadata only" in line for line in lines)
    assert any(entry["doc_url"] in line for line in lines)
    close_db()


def test_refresh_skips_8k_without_exhibit_and_unsupported_forms(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    plain_8k = _filing(form="8-K", accession="0000320193-26-000080")
    s1 = _filing(form="S-1", accession="0000320193-26-000070")
    excerpt_calls = []

    counts = filing_briefs.refresh(
        ["AAPL"],
        listing_fetcher=_listing_fetcher({"AAPL": [plain_8k, s1]}, []),
        excerpt_fetcher=_excerpt_fetcher({plain_8k["accession"]: None}, excerpt_calls),
        caller=_ok_summary,
    )

    assert excerpt_calls == [plain_8k["accession"]]  # the S-1 is out of scope entirely
    assert counts["new_documents"] == 0
    assert filing_briefs.briefs(["AAPL"], as_of=datetime(2026, 8, 4, tzinfo=UTC)) == {}
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM filing_documents").fetchone()[0] == 0
        assert conn.execute("SELECT status FROM filing_scan_status WHERE ticker='AAPL'").fetchone()[0] == "ok"
    close_db()


def test_summarizer_receives_only_the_excerpt_text(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    filing = _filing()
    document = _document(filing, excerpt="Only this bounded narrative excerpt is disclosed.")
    captured = []

    def caller(system, user):
        captured.append((system, user))
        return _ok_summary()

    filing_briefs.refresh(
        ["AAPL"],
        listing_fetcher=_listing_fetcher({"AAPL": [filing]}, []),
        excerpt_fetcher=_excerpt_fetcher({filing["accession"]: document}, []),
        caller=caller,
    )

    assert len(captured) == 1
    system, user = captured[0]
    assert "UNTRUSTED" in system
    assert "Only this bounded narrative excerpt is disclosed." in user
    assert document["doc_url"] not in user
    assert "0000320193" not in user
    close_db()


def test_disabled_pipeline_fetches_nothing_and_reads_nothing(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    from settings import load_settings

    settings = load_settings({"FILING_BRIEFS_ENABLED": "false"})

    def forbidden(*_args, **_kwargs):
        raise AssertionError("disabled pipeline must not fetch")

    counts = filing_briefs.refresh(["AAPL"], settings=settings, listing_fetcher=forbidden, excerpt_fetcher=forbidden)
    assert counts == {"scanned": 0, "cached": 0, "empty": 0, "failed": 0, "new_documents": 0}
    assert filing_briefs.briefs(["AAPL"], as_of=datetime(2026, 8, 4, tzinfo=UTC), settings=settings) == {}
    close_db()


def test_briefs_are_read_only_and_never_fetch(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)

    assert filing_briefs.briefs(["AAPL", "MSFT"], as_of=datetime(2026, 8, 4, tzinfo=UTC)) == {}
    close_db()


def test_prompt_lines_render_quoted_dated_source_attributed_briefs(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    filing = _filing()
    filing_briefs.refresh(
        ["AAPL"],
        listing_fetcher=_listing_fetcher({"AAPL": [filing]}, []),
        excerpt_fetcher=_excerpt_fetcher({filing["accession"]: _document(filing)}, []),
        caller=_ok_summary,
    )

    lines = filing_briefs.prompt_lines(filing_briefs.briefs(["AAPL"], as_of=datetime(2026, 8, 4, tzinfo=UTC)))

    assert lines[0].startswith("  AAPL | 10-Q filed 2026-07-31 | guidance: raised | tone: positive")
    assert '"Revenue grew 10%"; "Services margin expanded"' in lines[1]
    assert '"Supply constraints"' in lines[2]
    assert 'outlook: "Next quarter revenue guided higher"' in lines[3]
    assert 'capital allocation: "New $10B buyback"' in lines[4]
    assert lines[5] == f"    source: {filing['link']}"
    close_db()


def test_long_excerpts_are_summarised_per_chunk_and_merged(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    monkeypatch.setattr(filing_briefs, "_SUMMARY_CHUNK_CHARS", 120)
    filing = _filing()
    paragraphs = [f"Paragraph {index} discusses segment results and margins in some detail." for index in range(8)]
    document = _document(filing, excerpt="\n".join(paragraphs))
    calls = []

    def caller(system, user):
        calls.append(user)
        if len(calls) > 2:
            return json.dumps({"status": "insufficient_text"})
        if len(calls) == 1:
            return json.dumps(
                {
                    "status": "ok",
                    "guidance": "raised",
                    "key_points": ["Revenue grew", "Margins expanded"],
                    "risks": ["Supply constraints"],
                    "outlook": "Next quarter revenue guided higher",
                    "capital_allocation": "New $10B buyback",
                    "tone": "positive",
                }
            )
        return json.dumps(
            {
                "status": "ok",
                "guidance": "lowered",
                "key_points": ["Margins expanded", "Costs fell"],
                "risks": ["Supply constraints", "FX headwinds"],
                "outlook": "",
                "capital_allocation": "",
                "tone": "negative",
            }
        )

    filing_briefs.refresh(
        ["AAPL"],
        listing_fetcher=_listing_fetcher({"AAPL": [filing]}, []),
        excerpt_fetcher=_excerpt_fetcher({filing["accession"]: document}, []),
        caller=caller,
    )

    assert len(calls) > 1
    assert all("part" in user for user in calls)
    brief = filing_briefs.briefs(["AAPL"], as_of=datetime(2026, 8, 4, tzinfo=UTC))["AAPL"][0]["brief"]
    assert brief["guidance"] == "unknown"  # conflicting chunk guidance never merges to a direction
    assert brief["key_points"] == ["Revenue grew", "Margins expanded", "Costs fell"]  # deduped, order kept
    assert brief["risks"] == ["Supply constraints", "FX headwinds"]
    assert brief["outlook"] == "Next quarter revenue guided higher"
    assert brief["capital_allocation"] == "New $10B buyback"
    assert brief["tone"] == "neutral"  # tied vote across ok chunks
    close_db()


def test_chunk_failures_degrade_to_metadata_only(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    monkeypatch.setattr(filing_briefs, "_SUMMARY_CHUNK_CHARS", 120)
    filing = _filing()
    document = _document(filing, excerpt="\n".join(f"Paragraph {index} of narrative text." for index in range(8)))

    filing_briefs.refresh(
        ["AAPL"],
        listing_fetcher=_listing_fetcher({"AAPL": [filing]}, []),
        excerpt_fetcher=_excerpt_fetcher({filing["accession"]: document}, []),
        caller=lambda *_: None,
    )

    result = filing_briefs.briefs(["AAPL"], as_of=datetime(2026, 8, 4, tzinfo=UTC))
    assert result["AAPL"][0]["status"] == "metadata_only"
    close_db()


def test_default_caller_uses_the_local_pi_agent(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    from adapters.llm.pi_copilot import PiCompletion
    from settings import load_settings

    completed = []

    class FakePiClient:
        @classmethod
        def from_settings(cls, settings):
            return cls()

        def complete(self, model, system_prompt, user_prompt):
            completed.append((model, system_prompt, user_prompt))
            return PiCompletion(text=_ok_summary(), session_id="s", usage_json="{}", estimated_cost_usd=0.0)

    monkeypatch.setattr(filing_briefs, "PiCopilotClient", FakePiClient)
    filing = _filing()
    settings = load_settings({"PI_COPILOT_JUDGE_MODEL": "judge-d"})

    filing_briefs.refresh(
        ["AAPL"],
        settings=settings,
        listing_fetcher=_listing_fetcher({"AAPL": [filing]}, []),
        excerpt_fetcher=_excerpt_fetcher({filing["accession"]: _document(filing)}, []),
    )

    assert [call[0] for call in completed] == ["judge-d"]
    result = filing_briefs.briefs(["AAPL"], as_of=datetime(2026, 8, 4, tzinfo=UTC))
    assert result["AAPL"][0]["brief"]["guidance"] == "raised"
    with get_db() as conn:
        assert conn.execute("SELECT model_name FROM filing_briefs").fetchone()[0] == "judge-d"
    close_db()


def test_default_caller_honours_the_configured_summary_model_and_pi_failures(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    from adapters.llm.pi_copilot import PiCopilotError
    from settings import load_settings

    completed = []

    class FailingPiClient:
        @classmethod
        def from_settings(cls, settings):
            return cls()

        def complete(self, model, system_prompt, user_prompt):
            completed.append(model)
            raise PiCopilotError("pi unavailable")

    monkeypatch.setattr(filing_briefs, "PiCopilotClient", FailingPiClient)
    filing = _filing()
    settings = load_settings({"FILING_SUMMARY_MODEL": "kimi-k3"})

    filing_briefs.refresh(
        ["AAPL"],
        settings=settings,
        listing_fetcher=_listing_fetcher({"AAPL": [filing]}, []),
        excerpt_fetcher=_excerpt_fetcher({filing["accession"]: _document(filing)}, []),
    )

    assert completed == ["kimi-k3"]
    result = filing_briefs.briefs(["AAPL"], as_of=datetime(2026, 8, 4, tzinfo=UTC))
    assert result["AAPL"][0]["status"] == "metadata_only"
    close_db()
