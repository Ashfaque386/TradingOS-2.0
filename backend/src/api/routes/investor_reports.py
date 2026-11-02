"""Investor Reporting Agent API (Phase 19, docs/phase19-audit.md Part
2.5/3.3): read-only browsing of real, already-generated reports, plus a
manual "generate now" trigger for ops visibility -- mirrors
`live_trading.py`'s "simulate a tick" precedent (exercises the exact same
`generate_investor_report` the scheduler calls automatically).
"""

import uuid
from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.engine.paper_trading.market_hours import IST
from src.models.investor_report import InvestorReport
from src.models.user import User
from src.orchestration.investor_report_period import ReportPeriodError, check_report_period
from src.orchestration.investor_reporting import generate_investor_report

router = APIRouter(prefix="/investor-reports", tags=["investor-reports"])

_OPERATOR_ROLES = [Role.SYSTEM_ADMINISTRATOR, Role.PORTFOLIO_MANAGER]

register_policy("GET", "/api/v1/investor-reports", roles=list(Role))
register_policy("GET", "/api/v1/investor-reports/{report_id}", roles=list(Role))
register_policy("POST", "/api/v1/investor-reports/generate", roles=_OPERATOR_ROLES)


class InvestorReportSummaryResponse(BaseModel):
    id: uuid.UUID
    period_start: str
    period_end: str
    cadence: str
    generated_at: str


class InvestorReportResponse(InvestorReportSummaryResponse):
    content_markdown: str
    generated_by: str


class GenerateInvestorReportRequest(BaseModel):
    period_start: date
    period_end: date
    cadence: Literal["weekly", "monthly"] = "weekly"


def _summary(report: InvestorReport) -> InvestorReportSummaryResponse:
    return InvestorReportSummaryResponse(
        id=report.id,
        period_start=report.period_start.isoformat(),
        period_end=report.period_end.isoformat(),
        cadence=report.cadence,
        generated_at=report.generated_at.isoformat(),
    )


@router.get("")
async def list_investor_reports_endpoint(
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> list[InvestorReportSummaryResponse]:
    result = await db.execute(select(InvestorReport).order_by(InvestorReport.period_end.desc()))
    return [_summary(r) for r in result.scalars().all()]


@router.get("/{report_id}")
async def get_investor_report_endpoint(
    report_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> InvestorReportResponse:
    report = await db.get(InvestorReport, report_id)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such investor report")
    return InvestorReportResponse(
        **_summary(report).model_dump(),
        content_markdown=report.content_markdown,
        generated_by=report.generated_by,
    )


@router.post("/generate", status_code=status.HTTP_201_CREATED)
async def generate_investor_report_endpoint(
    body: GenerateInvestorReportRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> InvestorReportResponse:
    """Writes a report for the period, which must be one that can be reported on: it ends on or
    after the day it starts, is not in the future in India, and fits its cadence (a week, or a
    month). Asking twice for the same period writes a second report, on purpose: more may have
    been filled since."""
    try:
        check_report_period(
            body.period_start, body.period_end, body.cadence, today=datetime.now(IST).date()
        )
    except ReportPeriodError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    report = await generate_investor_report(
        db,
        period_start=body.period_start,
        period_end=body.period_end,
        cadence=body.cadence,
        # The column holds 64 characters; a longer address is cut rather than refused.
        generated_by=f"user:{current_user.email}"[:64],
    )
    return InvestorReportResponse(
        **_summary(report).model_dump(),
        content_markdown=report.content_markdown,
        generated_by=report.generated_by,
    )
