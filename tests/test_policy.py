from __future__ import annotations

from pythonator.core.models import RestartPolicy
from pythonator.core.policy import MAX_DELAY, decide_restart
from pythonator.core.runner import ExitInfo


def crash(runtime: float = 1.0, code: int = 1) -> ExitInfo:
    return ExitInfo(code=code, requested=False, runtime=runtime)


def test_never_and_user_stop() -> None:
    assert decide_restart(RestartPolicy.NEVER, crash(), 0).delay is None
    stopped = ExitInfo(code=-15, requested=True, runtime=5)
    assert decide_restart(RestartPolicy.ALWAYS, stopped, 0).delay is None


def test_on_failure_ignores_clean_exit() -> None:
    assert decide_restart(RestartPolicy.ON_FAILURE, crash(code=0), 0).delay is None
    assert decide_restart(RestartPolicy.ALWAYS, crash(code=0), 0).delay is not None


def test_exponential_backoff_and_reset() -> None:
    failures, delays = 0, []
    for _ in range(9):
        d = decide_restart(RestartPolicy.ON_FAILURE, crash(), failures)
        assert d.delay is not None
        delays.append(d.delay)
        failures = d.failures
    assert delays[:5] == [1, 2, 4, 8, 16]
    assert delays[-1] == MAX_DELAY
    stable = decide_restart(RestartPolicy.ON_FAILURE, crash(runtime=3600), failures)
    assert stable.delay == 1 and stable.failures == 0
