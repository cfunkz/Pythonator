"""Service specs and approvals on disk.
Layout under the data folder (owner-only, 0700):
    services/<name>.json   the spec (each save bumps `generation`; the previous file is kept as .bak)
    approvals.json         {name: risk hash you approved}
    approved/<name>.json   the spec as you last approved it (what the next plan is compared with)
    proposed/<name>.json   the widening lines left waiting after "Apply safe steps only"
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from pythonator.model.service import NAME_RE, ServiceSpec
from pythonator.policy.risk import risk_hash
from pythonator.store.files import ensure_private_dir, read_json, write_atomic, write_json


class SpecStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.dir = ensure_private_dir(root / "services")
        self.approvals_path = root / "approvals.json"
        self.broken: dict[str, str] = {}  # file name -> why it couldn't be loaded

    def _path(self, name: str) -> Path:
        if not NAME_RE.fullmatch(name):
            raise ValueError(f"bad service name {name!r}")
        return self.dir / f"{name}.json"

    def load_all(self) -> dict[str, ServiceSpec]:
        out: dict[str, ServiceSpec] = {}
        self.broken.clear()
        for p in sorted(self.dir.glob("*.json")):
            try:
                spec = ServiceSpec.model_validate(read_json(p), strict=False)
            except (ValidationError, ValueError, OSError) as e:
                self.broken[p.name] = str(e).splitlines()[0]
                continue
            if f"{spec.name}.json" != p.name:
                self.broken[p.name] = "file name doesn't match the service name"
                continue
            out[spec.name] = spec
        return out

    def get(self, name: str) -> ServiceSpec | None:
        p = self._path(name)
        if not p.exists():
            return None
        return ServiceSpec.model_validate(read_json(p), strict=False)

    def save(self, spec: ServiceSpec) -> ServiceSpec:
        """Save as a new generation. Returns the saved spec (with its generation)."""
        old = self.get(spec.name)
        gen = (old.generation + 1) if old else 1
        spec = spec.evolve(generation=gen)
        write_atomic(self._path(spec.name), spec.model_dump_json(indent=1).encode(), backup=old is not None)
        return spec

    def set_desired(self, spec: ServiceSpec, desired: str) -> ServiceSpec:
        """Start/stop isn't a change to what the service may do, so it keeps its generation."""
        spec = spec.evolve(desired=desired)
        write_atomic(self._path(spec.name), spec.model_dump_json(indent=1).encode())
        return spec

    def remove(self, name: str) -> None:
        p = self._path(name)
        for f in (p, p.with_suffix(".json.bak"), self._approved_path(name), self._proposed_path(name)):
            f.unlink(missing_ok=True)
        a = self.approvals()
        if a.pop(name, None) is not None:
            write_json(self.approvals_path, a)

    # approvals -------------------------------------------------------------------------------------------------
    def approvals(self) -> dict[str, str]:
        data = read_json(self.approvals_path, {})
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(data, dict) else {}

    def approve(self, name: str, expected_hash: str) -> bool:
        """Approve the spec as it is on disk now, only if it still hashes to what you were shown."""
        spec = self.get(name)
        if spec is None or risk_hash(spec) != expected_hash:
            return False
        a = self.approvals()
        a[name] = expected_hash
        write_json(self.approvals_path, a)
        write_atomic(self._approved_path(name), spec.model_dump_json(indent=1).encode())  # what the next plan diffs against
        return True

    def _approved_path(self, name: str) -> Path:
        return ensure_private_dir(self.root / "approved") / self._path(name).name

    def approved_spec(self, name: str) -> ServiceSpec | None:
        """The spec as it was when you last approved it (None for a service never approved)."""
        try:
            return ServiceSpec.model_validate(read_json(self._approved_path(name)), strict=False)
        except (ValidationError, ValueError, OSError):
            return None

    def is_approved(self, spec: ServiceSpec) -> bool:
        return self.approvals().get(spec.name) == risk_hash(spec)

    # proposals: what "Apply safe steps only" left waiting ---------------------------------------------------------
    def _proposed_path(self, name: str) -> Path:
        return ensure_private_dir(self.root / "proposed") / self._path(name).name

    def propose(self, spec: ServiceSpec) -> None:
        write_atomic(self._proposed_path(spec.name), spec.model_dump_json(indent=1).encode())

    def proposal(self, name: str) -> ServiceSpec | None:
        try:
            return ServiceSpec.model_validate(read_json(self._proposed_path(name)), strict=False)
        except (ValidationError, ValueError, OSError):
            return None

    def drop_proposal(self, name: str) -> None:
        self._proposed_path(name).unlink(missing_ok=True)
