"""Application runtime lifecycle behavior."""

import asyncio

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


def test_app_factory_owns_the_injected_runtime_instance():
    runtime = AppRuntime(scheduler=Scheduler(), decision_batch_runner=DecisionBatchRunner())

    app = create_app(runtime)

    assert app.state.runtime is runtime
