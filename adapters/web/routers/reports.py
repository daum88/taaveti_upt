"""Monthly report HTTP adapter."""

import asyncio
import re
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from adapters.web.schemas.common import error_responses
from adapters.web.schemas.reports import MonthlyReportResponse, ReportAccount, ReportAnalysisResponse
from services import report_narrative, reporting

router = APIRouter(tags=["reports"], responses=error_responses(500))

_MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _resolve_period(month: str | None, start: date | None, end: date | None) -> tuple[date, date]:
    """Resolve month=YYYY-MM or an explicit start/end pair into an inclusive date range."""
    today = datetime.now(UTC).date()
    if month is not None and (start is not None or end is not None):
        raise HTTPException(status_code=422, detail="Pass either month or start/end, not both.")
    if month is not None:
        if not _MONTH_PATTERN.match(month):
            raise HTTPException(status_code=422, detail="month must be formatted as YYYY-MM.")
        year, month_number = (int(part) for part in month.split("-"))
        first = date(year, month_number, 1)
        last = date(year + (month_number == 12), month_number % 12 + 1, 1)
        resolved_start, resolved_end = first, last - timedelta(days=1)
    else:
        if start is None or end is None:
            raise HTTPException(status_code=422, detail="Pass month or both start and end.")
        resolved_start, resolved_end = start, end
    if resolved_start > resolved_end:
        raise HTTPException(status_code=422, detail="start must not be after end.")
    if resolved_end > today:
        resolved_end = today
    if resolved_start > today:
        raise HTTPException(status_code=422, detail="Period lies entirely in the future.")
    return resolved_start, resolved_end


@router.get("/api/reports/accounts", response_model=list[ReportAccount])
async def report_accounts():
    return await asyncio.to_thread(reporting.available_accounts)


@router.get("/api/reports/monthly", response_model=MonthlyReportResponse, responses=error_responses(404, 422))
async def monthly_report(
    user_id: Annotated[int, Query()],
    month: Annotated[str | None, Query()] = None,
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
):
    start_date, end_date = _resolve_period(month, start, end)
    report = await asyncio.to_thread(reporting.build_report, user_id, start_date, end_date)
    if report is None:
        raise HTTPException(status_code=404, detail=f"Unknown account: {user_id}")
    return report


@router.get("/api/reports/analysis", response_model=ReportAnalysisResponse, responses=error_responses(404, 422, 503))
async def report_analysis(
    user_id: Annotated[int, Query()],
    month: Annotated[str | None, Query()] = None,
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
):
    """LLM-written assessment of why the account performed as it did in the period."""
    start_date, end_date = _resolve_period(month, start, end)
    analysis = await asyncio.to_thread(report_narrative.build_analysis, user_id, start_date, end_date)
    if analysis is None:
        raise HTTPException(status_code=404, detail=f"Unknown account: {user_id}")
    if analysis["narrative"] is None:
        raise HTTPException(status_code=503, detail="LLM provider unavailable or returned no assessment.")
    return analysis
