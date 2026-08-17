"""Application runtime lifecycle behavior."""

import asyncio

from fastapi import WebSocketDisconnect

from adapters.web.app import create_app
from adapters.web.runtime import AppRuntime


class Scheduler:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0

    def start(self) -> None:
        self.starts += 1

    def stop(self) -> None:
        self.stops += 1

    @staticmethod
    def status() -> dict:
        return {"running": True}


class DecisionBatchRunner:
    def __init__(self) -> None:
        self.recoveries = 0

    def recover_interrupted(self) -> None:
        self.recoveries += 1


def test_runtime_owns_scheduler_and_background_task_lifecycle():
    scheduler = Scheduler()
    decision_batch_runner = DecisionBatchRunner()
    runtime = AppRuntime(scheduler=scheduler, decision_batch_runner=decision_batch_runner)

    async def verify() -> None:
        await runtime.start()
        await runtime.start()

        assert scheduler.starts == 1
        assert decision_batch_runner.recoveries == 1
        assert runtime.status() == {"running": True}

        await runtime.stop()

    asyncio.run(verify())

    assert scheduler.stops == 1


def test_websocket_connection_does_not_duplicate_the_initial_leaderboard_query():
    calls = 0

    class Queries:
        @staticmethod
        def leaderboard():
            nonlocal calls
            calls += 1
            return []

    class Socket:
        accepted = False

        async def accept(self):
            self.accepted = True

        @staticmethod
        async def receive_text():
            raise WebSocketDisconnect()

    socket = Socket()
    runtime = AppRuntime(
        scheduler=Scheduler(),
        decision_batch_runner=DecisionBatchRunner(),
        portfolio_queries=Queries(),
    )

    asyncio.run(runtime.serve_websocket(socket))

    assert socket.accepted is True
    assert calls == 0


def test_app_factory_owns_the_injected_runtime_instance():
    runtime = AppRuntime(scheduler=Scheduler(), decision_batch_runner=DecisionBatchRunner())

    app = create_app(runtime)

    assert app.state.runtime is runtime
