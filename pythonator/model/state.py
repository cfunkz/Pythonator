"""What is really there (observed) and how it differs from the spec (drift)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class Phase(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    BACKOFF = "backoff"  # crashed, waiting to restart
    FAILED = "failed"  # gave up after too many crashes
    BLOCKED = "blocked"  # can't start until something is fixed (refusal, port taken, missing tool)

class Observed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    phase: Phase = Phase.STOPPED
    pid: int | None = None
    started_at: float | None = None
    exit_code: int | None = None
    restarts: int = 0
    next_retry_at: float | None = None
    cpu_percent: float = 0.0
    memory_bytes: int = 0
    tasks: int = 0
    message: str = ""
    generation: int = 0  # the spec generation this run was started from
    health: str = ""  # "", "ok", "failing 1/3"

class DriftKind(StrEnum):
    MISSING = "missing"  # should run, doesn't
    EXTRA = "extra"  # runs or exists, nothing wants it
    CHANGED = "changed"  # running from an older generation
    BLOCKED = "blocked"  # can't converge without you
    UNHEALTHY = "unhealthy"

class Drift(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    kind: DriftKind
    detail: str
    fixes: tuple[str, ...] = ()  # e.g. ("heal", "adopt", "ignore")
