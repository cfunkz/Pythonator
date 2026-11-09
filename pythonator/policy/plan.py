"""The EnforcementPlan: one object that says what protects a service on this machine.
The settings dialog, the (i) cards, the first console line, System check and the docs all read this object, so
they can't disagree. A layer is never "cooperative": it is enforced, reduced (weaker, labelled), not enforced
(labelled, allowed only where the policy says so) or the start is refused."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Status(StrEnum):
    ENFORCED = "enforced"
    REDUCED = "reduced"
    NOT_ENFORCED = "not-enforced"
    OFF = "off"  # the setting doesn't ask for it (e.g. no ports opened)

@dataclass(frozen=True)
class Layer:
    key: str  # tree, limits, files, syscalls, network, loopback, ports
    title: str
    status: Status
    by: str  # what enforces it, in words a person can look up ("cgroup scope", "Seatbelt profile")
    detail: str = ""

@dataclass(frozen=True)
class EnforcementPlan:
    os: str
    mode: str
    layers: tuple[Layer, ...] = field(default_factory=tuple)

    def layer(self, key: str) -> Layer | None:
        return next((x for x in self.layers if x.key == key), None)

    def first_line(self) -> str:
        """The first console line of every run."""
        parts = [f"{self.mode.capitalize()}"]
        for x in self.layers:
            if x.status is Status.OFF:
                continue
            mark = {Status.ENFORCED: "", Status.REDUCED: " (reduced)", Status.NOT_ENFORCED: " (not enforced)"}[x.status]
            parts.append(f"{x.title}: {x.by}{mark}")
        return " · ".join(parts)

    def weakest(self) -> Status:
        order = [Status.NOT_ENFORCED, Status.REDUCED, Status.ENFORCED]
        present = {x.status for x in self.layers if x.status is not Status.OFF}
        return next((s for s in order if s in present), Status.ENFORCED)

class Refused(Exception):
    """A start that can't be made safe on this machine. Carries a code, a sentence, and the fix."""

    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.fix = fix
