"""Decision-batch response contracts."""

from adapters.web.schemas.common import ResponseModel


class DecisionBatchCounts(ResponseModel):
    total: int
    completed: int
    failed: int


class DecisionBatchAgent(ResponseModel):
    status: str
    completed_at: str | None
    error: str | None
    trade_count: int


class DecisionBatchStatus(ResponseModel):
    batch_id: int | None
    status: str
    last_triggered_at: str | None
    last_completed_at: str | None
    next_eligible_at: str | None
    counts: DecisionBatchCounts
    agents: dict[str, DecisionBatchAgent]
    error: str | None = None


class DecisionSchedule(ResponseModel):
    kind: str
    weekdays: list[int]
    time: str


class DecisionDay(ResponseModel):
    date: str
    weekday: str
    is_today: bool
    state: str
    due_at: str | None
    batch: DecisionBatchStatus | None
    run_count: int


class DecisionWeekResponse(ResponseModel):
    week_start: str
    timezone: str
    schedule: DecisionSchedule
    days: list[DecisionDay]
    current_batch: DecisionBatchStatus | None
    latest_batch: DecisionBatchStatus | None
    next_reminder_at: str | None
    ai_account_count: int
