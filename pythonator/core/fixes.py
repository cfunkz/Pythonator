"""Recovering from "that version doesn't exist" install failures.

When ``pip install`` fails on a pin like ``numpy==1.26.0`` (often: no build for this Python),
pip lists the versions it *could* install. We pick the closest one, and the supervisor asks
the user before changing anything. For Node package managers the next best option is the
latest release, which they write back to ``package.json`` themselves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Runtime

_PIP_VERSIONS = re.compile(
    r"Could not find a version that satisfies the requirement (?P<req>\S+) \(from versions: (?P<versions>[^)]*)\)"
)
_PIP_NO_MATCH = re.compile(r"No matching distribution found for (?P<req>\S+)")
_JS_NO_MATCH = (
    re.compile(r"No matching version found for (?P<name>@?[^@\s]+)@(?P<ver>[^\s.]+(?:\.[^\s.]+)*)\.?"),  # npm/pnpm
    re.compile(r'Couldn\'t find any versions for "(?P<name>[^"]+)" that matches "(?P<ver>[^"]+)"'),  # yarn
    re.compile(r'No version matching "(?P<ver>[^"]+)" found for specifier "(?P<name>[^"]+)"'),  # bun
)
_BUILD_FAILED = (
    re.compile(r"Failed building wheel for (?P<name>[A-Za-z0-9._-]+)"),
    re.compile(r"Failed to build (?:installable wheels for some pyproject\.toml based projects \()?"
               r"(?P<name>[A-Za-z0-9._-]+)"),
)
_COLLECTING = re.compile(r"Collecting (?P<name>[A-Za-z0-9._-]+)(?:\[[^\]]*\])?==(?P<ver>[^\s;,()]+)")
_METADATA_FAILED = "metadata-generation-failed"
_REQ = re.compile(r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?P<extras>\[[^\]]*\])?\s*(?P<spec>[^;#]*)")
_NODE_RUNTIMES = (Runtime.NODE, Runtime.BUN)


@dataclass(frozen=True, slots=True)
class DependencyFix:
    package: str  # as written, e.g. "numpy" or "@discordjs/rest"
    old: str  # the failing requirement, e.g. "numpy==1.26.0"
    new: str  # what to use instead, e.g. "numpy==2.1.0" or "express@latest"
    reason: str  # one sentence for the console

    @property
    def question(self) -> str:
        return f"{self.reason} Use {self.new} instead and install again? Type y or n."


@dataclass(frozen=True, slots=True)
class VersionProbe:
    """A pinned package failed to *build*: ask pip which versions have ready-made wheels first."""

    package: str
    old: str  # e.g. "numpy==1.26.0"
    wanted: str  # e.g. "1.26.0"

    def pip_args(self) -> list[str]:
        # Asking for a version that can't exist makes pip list every binary-compatible one.
        return ["install", "--dry-run", "--only-binary=:all:", "--no-deps", "--disable-pip-version-check",
                f"{self.package}==0.0.0.dev0"]


def normalize(name: str) -> str:
    """PEP 503 name normalisation: ``Foo_Bar.baz`` and ``foo-bar-baz`` are the same project."""
    return re.sub(r"[-_.]+", "-", name).lower()


def suggest_fix(runtime: Runtime, output: list[str]) -> DependencyFix | VersionProbe | None:
    """Look at an install's output and propose a replacement for the requirement that failed."""
    if runtime is Runtime.PYTHON:
        return _pip_fix(output) or _build_failure(output)
    if runtime in _NODE_RUNTIMES:
        return _js_fix(output)
    return None


def _pip_fix(output: list[str]) -> DependencyFix | None:
    failed: str | None = None
    versions: list[str] | None = None
    for line in output:
        if m := _PIP_VERSIONS.search(line):
            failed = m.group("req")
            raw = m.group("versions").strip()
            versions = [] if raw == "none" else [v.strip() for v in raw.split(",") if v.strip()]
        elif (m := _PIP_NO_MATCH.search(line)) and failed is None:
            failed = m.group("req")
    if failed is None:
        return None
    req = _REQ.match(failed)
    if req is None:
        return None
    name, wanted = req.group("name"), _pinned(req.group("spec"))
    if versions is None:  # pip didn't list alternatives: the next best is "whatever fits this Python"
        if not req.group("spec").strip():
            return None
        return DependencyFix(name, failed, name, f"{failed} isn't available here; the newest compatible "
                             f"version of {name} may be.")
    best = closest_version(wanted, versions)
    if best is None:
        return None
    reason = (f"{name} {wanted} isn't available for this Python." if wanted
              else f"Nothing matches {failed} for this Python.")
    return DependencyFix(name, failed, f"{name}=={best}", f"{reason} The closest available version is {best}.")


def _build_failure(output: list[str]) -> VersionProbe | None:
    failed: str | None = None
    for line in output:
        for pattern in _BUILD_FAILED:
            if m := pattern.search(line):
                failed = m.group("name")
    pins = {normalize(m.group("name")): (m.group("name"), m.group("ver"))
            for line in output if (m := _COLLECTING.search(line))}
    if failed is None and any(_METADATA_FAILED in line for line in output):
        # pip doesn't name the package here; it's the last one it was collecting.
        last = next((m for line in reversed(output) if (m := _COLLECTING.search(line))), None)
        failed = last.group("name") if last else None
    if failed is None or normalize(failed) not in pins:
        return None
    name, version = pins[normalize(failed)]
    return VersionProbe(name, f"{name}=={version}", version)


def fix_from_probe(probe: VersionProbe, output: list[str]) -> DependencyFix | None:
    """Turn the version list pip printed for ``probe`` into a fix."""
    for line in output:
        if (m := _PIP_VERSIONS.search(line)) and m.group("versions").strip() != "none":
            versions = [v.strip() for v in m.group("versions").split(",") if v.strip()]
            best = closest_version(probe.wanted, [v for v in versions if v != probe.wanted])
            if best:
                return DependencyFix(probe.package, probe.old, f"{probe.package}=={best}",
                                     f"{probe.old} has no ready-made build for this Python and building it "
                                     f"failed. The closest version with one is {best}.")
    return None


def _js_fix(output: list[str]) -> DependencyFix | None:
    for line in output:
        for pattern in _JS_NO_MATCH:
            if m := pattern.search(line):
                name, ver = m.group("name"), m.group("ver").rstrip(".")
                return DependencyFix(name, f"{name}@{ver}", f"{name}@latest",
                                     f"{name} has no version {ver}.")
    return None


def _pinned(spec: str) -> str:
    m = re.match(r"\s*(?:==|~=|===)\s*([^\s,;]+)", spec)
    return m.group(1).rstrip(".*") if m else ""


def _release(version: str) -> tuple[int, ...]:
    m = re.match(r"v?(\d+(?:\.\d+)*)", version.strip())
    if not m:
        return ()
    parts = [int(p) for p in m.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:  # 2.1 == 2.1.0
        parts.pop()
    return tuple(parts)


def _is_prerelease(version: str) -> bool:
    return bool(re.search(r"(a|b|rc|alpha|beta|dev|pre)\d*", version.lower().split("+")[0].lstrip("v")))


def closest_version(wanted: str, available: list[str]) -> str | None:
    """The smallest available release at or above ``wanted``, else the newest one below it."""
    stable = [v for v in available if _release(v) and not _is_prerelease(v)]
    pool = stable or [v for v in available if _release(v)]
    if not pool:
        return None
    target = _release(wanted)
    if target:
        higher = [v for v in pool if _release(v) >= target]
        if higher:
            return min(higher, key=_release)
    return max(pool, key=_release)


def apply_to_requirements(text: str, fix: DependencyFix, runtime: Runtime) -> str | None:
    """``text`` (a requirements.txt or a service's package list) with ``fix`` applied, or None."""
    lines = text.splitlines()
    changed = False
    for i, line in enumerate(lines):
        if runtime is Runtime.PYTHON:
            m = _REQ.match(line)
            if m and normalize(m.group("name")) == normalize(fix.package) and not line.lstrip().startswith("-"):
                version = fix.new.partition("==")[2]
                rest = line[m.end():].strip()  # environment markers and comments are kept
                pin = f"=={version}" if version else ""
                lines[i] = f"{m.group('name')}{m.group('extras') or ''}{pin}" + (f" {rest}" if rest else "")
                changed = True
        elif _js_name(line) == fix.package:
            lines[i] = fix.package  # unpinned: the package manager installs the latest
            changed = True
    if not changed:
        return None
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def _js_name(line: str) -> str:
    spec = line.strip()
    if not spec or spec.startswith("#"):
        return ""
    at = spec.find("@", 1)  # skip a scope's leading "@"
    return spec if at < 0 else spec[:at]
