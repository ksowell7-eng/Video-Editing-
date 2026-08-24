"""Run workspace and step state.

Every step writes into runs/<job-id>/<step>/ and records an entry in
state.json: status, the config fingerprint it ran under, its declared outputs,
and any payload the next step needs. A step is skipped when its recorded
fingerprint still matches and every declared output is still on disk. That
matters because two steps in this pipeline cost real money — a re-run after a
caption tweak must not re-bill Seedance or ElevenLabs.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import logs


@dataclass
class StepRecord:
    name: str
    status: str = "pending"          # pending | ok | failed | skipped
    fingerprint: str = ""
    outputs: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    duration_s: float = 0.0
    finished_at: float = 0.0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "fingerprint": self.fingerprint,
            "outputs": self.outputs,
            "data": self.data,
            "duration_s": round(self.duration_s, 2),
            "finished_at": self.finished_at,
            "error": self.error,
        }


class RunState:
    def __init__(self, root: Path, job_id: str):
        self.root = root
        self.job_id = job_id
        self.file = root / "state.json"
        self.steps: dict[str, StepRecord] = {}
        self.root.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        if not self.file.exists():
            return
        try:
            blob = json.loads(self.file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logs.warn("state.json unreadable; starting from a clean run state")
            return
        for name, rec in blob.get("steps", {}).items():
            self.steps[name] = StepRecord(name=name, **{
                k: v for k, v in rec.items()
                if k in {"status", "fingerprint", "outputs", "data", "duration_s", "finished_at", "error"}
            })

    def save(self) -> None:
        payload = {
            "job_id": self.job_id,
            "updated_at": time.time(),
            "steps": {name: rec.to_dict() for name, rec in self.steps.items()},
        }
        tmp = self.file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        tmp.replace(self.file)

    # ---- per-step helpers ----------------------------------------------

    def dir_for(self, step: str) -> Path:
        d = self.root / step
        d.mkdir(parents=True, exist_ok=True)
        return d

    def record(self, step: str) -> StepRecord:
        return self.steps.setdefault(step, StepRecord(name=step))

    def data(self, step: str) -> dict[str, Any]:
        """Payload a previous step published. Empty dict if it never ran."""
        rec = self.steps.get(step)
        return rec.data if rec and rec.status in ("ok", "skipped") else {}

    def is_fresh(self, step: str, fingerprint: str) -> bool:
        rec = self.steps.get(step)
        if not rec or rec.status not in ("ok", "skipped") or rec.fingerprint != fingerprint:
            return False
        missing = [o for o in rec.outputs if not Path(o).exists()]
        if missing:
            logs.debug(f"{step}: output missing, will re-run", missing=missing[0])
            return False
        return True

    def complete(
        self,
        step: str,
        *,
        fingerprint: str,
        outputs: list[Path] | None = None,
        data: dict[str, Any] | None = None,
        duration_s: float = 0.0,
    ) -> None:
        rec = self.record(step)
        rec.status = "ok"
        rec.fingerprint = fingerprint
        rec.outputs = [str(p) for p in (outputs or [])]
        rec.data = data or {}
        rec.duration_s = duration_s
        rec.finished_at = time.time()
        rec.error = ""
        self.save()

    def fail(self, step: str, message: str) -> None:
        rec = self.record(step)
        rec.status = "failed"
        rec.error = message[:2000]
        rec.finished_at = time.time()
        self.save()

    def invalidate(self, steps: list[str]) -> None:
        for name in steps:
            if name in self.steps:
                self.steps[name].status = "pending"
        self.save()
