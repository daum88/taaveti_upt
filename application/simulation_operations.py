"""Runtime health and whole-simulation state transitions."""

import asyncio
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any, Protocol

from adapters.market_data.market_calendar import is_market_open
from adapters.market_data.yfinance_quotes import fetch_current_prices
from adapters.sqlite.connection import transaction
from adapters.sqlite.simulation_state import reset_mutable_simulation_state
from services.index_fund import seed_index_fund
from services.llm_agent import check_provider_health
from settings import Settings, load_settings


class RuntimeScheduler(Protocol):
    def status(self) -> dict[str, Any]: ...

    def exclusive_portfolio_operation(self) -> AbstractContextManager[None]: ...


@dataclass(frozen=True)
class SimulationReset:
    seeded_index_accounts: int


ProviderHealth = Callable[[], dict[str, Any]]
QuoteFetcher = Callable[[list[str]], dict[str, dict[str, Any]]]
IndexSeeder = Callable[..., bool]
StateResetter = Callable[[Any], list[int]]


class SimulationOperations:
    """Hide health probes and atomic reset orchestration behind two operations."""

    def __init__(
        self,
        scheduler: RuntimeScheduler,
        *,
        market_open: Callable[[], bool] = is_market_open,
        provider_health: ProviderHealth | None = None,
        quote_fetcher: QuoteFetcher = fetch_current_prices,
        state_resetter: StateResetter = reset_mutable_simulation_state,
        index_seeder: IndexSeeder | None = None,
        index_ticker: str | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or load_settings()
        self._scheduler = scheduler
        self._market_open = market_open
        self._provider_health = provider_health or (lambda: check_provider_health(settings=self._settings))
        self._quote_fetcher = quote_fetcher
        self._state_resetter = state_resetter
        self._index_seeder = index_seeder or partial(seed_index_fund, settings=self._settings)
        self._index_ticker = (index_ticker or self._settings.index_fund_ticker).upper()

    async def health(self) -> dict[str, Any]:
        market_open, provider = await asyncio.gather(
            asyncio.to_thread(self._market_open),
            asyncio.to_thread(self._provider_health),
        )
        return {
            "market_open": market_open,
            "scheduler": self._scheduler.status(),
            "provider": provider,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def reset(self) -> SimulationReset:
        quote = self._quote_fetcher([self._index_ticker]).get(self._index_ticker, {})
        index_price = quote.get("price")
        seeded = 0
        with self._scheduler.exclusive_portfolio_operation(), transaction() as conn:
            index_user_ids = self._state_resetter(conn)
            if index_price:
                seeded = sum(self._index_seeder(user_id, price=index_price) for user_id in index_user_ids)
        return SimulationReset(seeded_index_accounts=seeded)
