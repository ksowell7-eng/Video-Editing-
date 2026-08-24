"""Step protocol and the shared run context."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from ..budget import Budget
from ..config import Job
from ..state import RunState
from .. import logs


@dataclass
class Context:
    job: Job
    state: RunState
    budget: Budget
    run_dir: Path
    force: bool = False
    dry_run: bool = False

    def dir_for(self, step: str) -> Path:
        return self.state.dir_for(step)

    def data(self, step: str) -> dict[str, Any]:
        return self.state.data(step)

    def require(self, step: str, key: str) -> Any:
        payload = self.data(step)
        if key not in payload:
            from ..errors import StepFailed  # noqa: PLC0415

            raise StepFailed(
                f"Step '{step}' has not published '{key}' yet",
                hint=f"Run the pipeline from '{step}' (--from {step}).",
            )
        return payload[key]


@dataclass
class StepResult:
    outputs: list[Path]
    data: dict[str, Any]


class Step(Protocol):
    name: str
    description: str

    def fingerprint(self, ctx: Context) -> str: ...

    def run(self, ctx: Context) -> StepResult: ...


@dataclass
class SimpleStep:
    name: str
    description: str
    sections: tuple[str, ...]
    fn: Callable[[Context], StepResult]
    depends_on: tuple[str, ...] = ()

    def fingerprint(self, ctx: Context) -> str:
        parts = [ctx.job.fingerprint(*self.sections)]
        # Fold in upstream fingerprints so a re-scraped article invalidates
        # everything built on top of it without any manual bookkeeping.
        for dependency in self.depends_on:
            record = ctx.state.steps.get(dependency)
            parts.append(record.fingerprint if record else "")
        return "-".join(parts)

    def run(self, ctx: Context) -> StepResult:
        return self.fn(ctx)


def execute(step: Step, ctx: Context) -> bool:
    """Run a step unless its recorded state is still fresh. Returns True if it ran."""
    fingerprint = step.fingerprint(ctx)
    if not ctx.force and ctx.state.is_fresh(step.name, fingerprint):
        logs.info(f"{step.name}: up to date")
        return False
    if ctx.dry_run:
        logs.info(f"{step.name}: would run — {step.description}")
        return False

    started = time.time()
    with logs.step(step.name, step.description):
        try:
            result = step.run(ctx)
        except Exception as exc:
            ctx.state.fail(step.name, str(exc))
            raise
    ctx.state.complete(
        step.name,
        fingerprint=fingerprint,
        outputs=result.outputs,
        data=result.data,
        duration_s=time.time() - started,
    )
    return True
