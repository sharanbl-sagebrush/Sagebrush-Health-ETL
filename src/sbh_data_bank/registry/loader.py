"""Loading source specs from YAML.

Specs live as YAML files today and will move into tenant-scoped database rows
later. Both are the same validated :class:`SourceSpec`, so nothing downstream
changes when they move.

Loading is strict on purpose. ``extra="forbid"`` on every model means a typo in
a spec key is a startup failure rather than a setting that silently does
nothing - which is what the previous ``settings.yaml`` did with its duplicated
``connection_timeout`` and ``pool_size`` keys, where the second value silently
won.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from sbh_data_bank.registry.spec import SourceSpec

__all__ = ["SpecLoadError", "SpecRegistry", "load_spec", "load_spec_dir"]


class SpecLoadError(ValueError):
    """A spec file is malformed. Names the file, since a validation error deep
    in a nested model is otherwise hard to place."""


def load_spec(path: str | Path) -> SourceSpec:
    """Load and validate one spec file."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SpecLoadError(f"{path.name}: not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise SpecLoadError(f"{path.name}: expected a mapping at the top level")
    try:
        return SourceSpec.model_validate(raw)
    except ValidationError as exc:
        raise SpecLoadError(f"{path.name}: {exc}") from exc


def load_spec_dir(directory: str | Path) -> list[SourceSpec]:
    """Load every ``.yaml`` spec in a directory, sorted by key."""
    directory = Path(directory)
    if not directory.is_dir():
        raise SpecLoadError(f"{directory}: not a directory")
    specs = [load_spec(p) for p in sorted(directory.glob("*.yaml"))]
    keys = [s.key for s in specs]
    duplicates = {k for k in keys if keys.count(k) > 1}
    if duplicates:
        raise SpecLoadError(f"{directory}: duplicate source keys {sorted(duplicates)}")
    return sorted(specs, key=lambda s: s.key)


class SpecRegistry:
    """Every known source spec, addressable by key.

    Filename matching returns *candidates* rather than a decision. Two sources
    can plausibly match one filename, and the header-based variant detector is
    what actually settles it - filenames in this estate are unreliable enough
    that they are never the last word.
    """

    __slots__ = ("_by_key",)

    def __init__(self, specs: list[SourceSpec]) -> None:
        self._by_key = {s.key: s for s in specs}

    @classmethod
    def from_dir(cls, directory: str | Path) -> SpecRegistry:
        return cls(load_spec_dir(directory))

    def get(self, key: str) -> SourceSpec | None:
        return self._by_key.get(key)

    def require(self, key: str) -> SourceSpec:
        spec = self._by_key.get(key)
        if spec is None:
            raise KeyError(f"no spec {key!r}; known: {sorted(self._by_key)}")
        return spec

    def candidates_for_filename(self, filename: str) -> list[SourceSpec]:
        return [s for s in self._by_key.values() if s.matches_filename(filename)]

    def expected_to_deliver(self) -> list[SourceSpec]:
        """Sources whose silence should raise a freshness alert."""
        return [s for s in self._by_key.values() if s.is_expected_to_deliver]

    def __len__(self) -> int:
        return len(self._by_key)

    def __iter__(self):
        return iter(sorted(self._by_key.values(), key=lambda s: s.key))
