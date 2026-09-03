"""Spend ledger for the paid steps.

One JSON file holds every charge this pipeline has ever made, and the cap is
enforced before the call goes out, not after the invoice arrives. Two caps
apply: per-run (this job's workspace) and lifetime total for the ledger file.

The reserve/settle split matters. `reserve()` writes a pending entry under an
exclusive lock *before* the HTTP request; `settle()` rewrites it with the real
cost once the provider reports it. If the process dies mid-generation the
pending entry survives, so a crashed run still counts against the cap instead
of silently freeing budget for an infinite retry loop.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .errors import BudgetExceeded
from . import logs

def _empty_ledger() -> dict[str, Any]:
    """A fresh ledger every time.

    This must not be a shared constant copied with dict(): that copy is
    shallow, so every "empty" ledger would share one entries list and spend
    would leak between unrelated budget files in the same process.
    """
    return {"version": 1, "entries": []}


@dataclass
class Reservation:
    id: str
    estimated_usd: float
    ledger: "Budget"

    def settle(self, actual_usd: float | None = None, **meta: Any) -> None:
        self.ledger._settle(self.id, actual_usd, meta)

    def release(self, reason: str = "failed") -> None:
        """Drop the hold — the provider never charged us."""
        self.ledger._settle(self.id, 0.0, {"released": reason})


class Budget:
    def __init__(self, path: Path, *, run_id: str, max_per_run: float, max_total: float,
                 fail_closed: bool = True):
        self.path = Path(path)
        self.run_id = run_id
        self.max_per_run = float(max_per_run)
        self.max_total = float(max_total)
        self.fail_closed = fail_closed
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ---- locked IO ------------------------------------------------------

    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        # Open r+ (create if absent) and hold an exclusive flock for the whole
        # read-modify-write so two concurrent runs can't both pass the cap.
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            raw = os.read(fd, 8 * 1024 * 1024).decode("utf-8").strip()
            try:
                ledger = json.loads(raw) if raw else _empty_ledger()
            except json.JSONDecodeError:
                logs.warn(f"budget file {self.path} was corrupt; quarantining and starting fresh")
                self.path.with_suffix(f".corrupt-{int(time.time())}.json").write_text(raw)
                ledger = _empty_ledger()
            ledger.setdefault("entries", [])
            yield ledger
            payload = json.dumps(ledger, indent=2).encode("utf-8")
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    # ---- queries --------------------------------------------------------

    @staticmethod
    def _sum(entries: list[dict], run_id: str | None = None) -> float:
        return sum(
            float(e.get("usd", 0.0))
            for e in entries
            if run_id is None or e.get("run_id") == run_id
        )

    def spent(self) -> tuple[float, float]:
        """(this run, all runs) in USD."""
        with self._locked() as ledger:
            return self._sum(ledger["entries"], self.run_id), self._sum(ledger["entries"])

    def remaining(self) -> float:
        run, total = self.spent()
        return min(self.max_per_run - run, self.max_total - total)

    # ---- mutation -------------------------------------------------------

    def reserve(self, description: str, estimated_usd: float | None, **meta: Any) -> Reservation:
        if estimated_usd is None:
            if self.fail_closed:
                raise BudgetExceeded(
                    f"No price known for '{description}' and budget.fail_closed is set",
                    hint="Set the provider's cost_per_second_usd, or set budget.fail_closed=false.",
                )
            estimated_usd = 0.0
        estimated_usd = float(estimated_usd)
        if estimated_usd < 0:
            raise BudgetExceeded(f"Negative cost estimate for '{description}'")

        entry_id = uuid.uuid4().hex[:12]
        with self._locked() as ledger:
            run_spent = self._sum(ledger["entries"], self.run_id)
            total_spent = self._sum(ledger["entries"])
            if run_spent + estimated_usd > self.max_per_run + 1e-9:
                raise BudgetExceeded(
                    f"'{description}' (${estimated_usd:.2f}) would put this run at "
                    f"${run_spent + estimated_usd:.2f}, over the ${self.max_per_run:.2f} per-run cap",
                    hint="Raise budget.max_usd_per_run, or shorten the generated segment.",
                )
            if total_spent + estimated_usd > self.max_total + 1e-9:
                raise BudgetExceeded(
                    f"'{description}' (${estimated_usd:.2f}) would put the ledger at "
                    f"${total_spent + estimated_usd:.2f}, over the ${self.max_total:.2f} lifetime cap",
                    hint=f"Raise budget.max_usd_total or start a new ledger file ({self.path}).",
                )
            ledger["entries"].append({
                "id": entry_id,
                "run_id": self.run_id,
                "description": description,
                "usd": estimated_usd,
                "state": "pending",
                "created_at": time.time(),
                **meta,
            })
        logs.cost(f"reserved ${estimated_usd:.2f} for {description}", run_total=f"${self.spent()[0]:.2f}")
        return Reservation(id=entry_id, estimated_usd=estimated_usd, ledger=self)

    def _settle(self, entry_id: str, actual_usd: float | None, meta: dict[str, Any]) -> None:
        with self._locked() as ledger:
            for entry in ledger["entries"]:
                if entry.get("id") == entry_id:
                    if actual_usd is not None:
                        entry["usd"] = round(float(actual_usd), 4)
                    entry["state"] = "settled"
                    entry["settled_at"] = time.time()
                    entry.update(meta)
                    break
        if actual_usd is not None:
            logs.cost(f"settled at ${float(actual_usd):.2f}", entry=entry_id)

    def summary(self) -> dict[str, Any]:
        run, total = self.spent()
        return {
            "ledger": str(self.path),
            "run_usd": round(run, 4),
            "total_usd": round(total, 4),
            "run_cap_usd": self.max_per_run,
            "total_cap_usd": self.max_total,
            "remaining_usd": round(min(self.max_per_run - run, self.max_total - total), 4),
        }


def from_job(job, run_id: str) -> Budget:
    cfg = job["budget"]
    return Budget(
        job.resolve(cfg["file"]),
        run_id=run_id,
        max_per_run=cfg["max_usd_per_run"],
        max_total=cfg["max_usd_total"],
        fail_closed=bool(cfg["fail_closed"]),
    )
