"""Schedules: when a scheduled service runs next, and what happens to missed runs."""

import json
import time
from datetime import datetime

from pythonator.model.service import ServiceSpec
from pythonator.reconcile.reconciler import Reconciler


def spec(tmp_path, catch_up=True):
    return ServiceSpec.model_validate_json(
        json.dumps(
            {
                "name": "job",
                "folder": str(tmp_path),
                "command": ["job.py"],
                "schedule": {"cron": "0 3 * * *", "catch_up": catch_up},
            }
        )
    )


def test_first_run_is_the_next_cron_time(tmp_path, host):
    r = Reconciler(tmp_path / "data", host)
    due = datetime.fromtimestamp(r.next_run(spec(tmp_path)))
    assert (due.hour, due.minute) == (3, 0) and due.timestamp() > time.time()


def test_missed_run_catches_up_once(tmp_path, host):
    r = Reconciler(tmp_path / "data", host)
    r.fired["job"] = time.time() - 3 * 86400  # the computer was off for three days
    assert r.next_run(spec(tmp_path)) < time.time()  # runs now, once
    assert r.next_run(spec(tmp_path, catch_up=False)) > time.time()  # skips to the next 03:00
