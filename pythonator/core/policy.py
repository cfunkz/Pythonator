"""Restart policy decisions (pure functions, no timers)."""

from __future__ import annotations

from dataclasses import dataclass

from .models import RestartPolicy
from .runner import ExitInfo

STABLE_AFTER = 30.0  # a run this long resets the crash-loop back-off
BASE_DELAY = 1.0
MAX_DELAY = 60.0


@dataclass(frozen=True, slots=True)
class RestartDecision:
    delay: float | None  # None: do not restart
    failures: int  # consecutive quick failures, carried into the next decision


def decide_restart(policy: RestartPolicy, info: ExitInfo, failures: int) -> RestartDecision:
    """Decide whether (and after how long) to restart a process that exited on its own.

    Quick consecutive exits back off exponentially (1s, 2s, 4s, ... capped at 60s) so a
    crash-looping bot doesn't burn CPU or hammer an API; a run that stayed up for
    :data:`STABLE_AFTER` seconds resets the back-off.
    """
    if info.requested or policy is RestartPolicy.NEVER:
        return RestartDecision(None, 0)
    if policy is RestartPolicy.ON_FAILURE and info.success:
        return RestartDecision(None, 0)
    failures = failures + 1 if info.runtime < STABLE_AFTER else 0
    delay = min(MAX_DELAY, BASE_DELAY * (2 ** max(0, failures - 1)))
    return RestartDecision(delay, failures)
