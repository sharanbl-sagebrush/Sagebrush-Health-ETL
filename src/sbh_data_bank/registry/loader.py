"""Loading source specs from YAML.

Specs live as YAML files today and will move into tenant-scoped database rows
later. Both are the same validated :class:`SourceSpec`, so nothing downstream
changes when they move.

Loading is strict on purpose. ``extra="forbid"`` on every model means a typo in
a spec key is a startup failure rather than a setting that silently does
nothing - which is what the previous ``settings.yaml`` did with its duplicated
``connection_timeout`` and ``pool_size`` keys, where the second value silently
won.

How this fits
-------------
Everything that needs the specs goes through ``SpecRegistry.from_dir``:
``scripts/migrate.py`` to emit one table per source, ``scripts/bind_real_files.py``
to prove each spec against the real extracts, and the shipped-spec tests. The
directory is ``specs/`` at the repository root. This module does no file
discovery beyond that directory and no header matching; it only turns YAML
into validated ``SourceSpec`` objects and refuses anything that does not
validate.
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
    """Load and validate one spec file.

    Returns the validated spec. Raises ``SpecLoadError``, naming the file,
    when the YAML will not parse, is not a mapping at the top level, or fails
    any of the model validators in ``registry.spec``.
    """
    path = Path(path)
    # safe_load, never load: a spec is data and must not be able to
    # construct arbitrary Python objects. utf-8 is explicit so a Windows
    # default code page cannot change how the file is read.
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SpecLoadError(f"{path.name}: not valid YAML: {exc}") from exc
    # An empty file parses as None and a stray list as a list; neither is a
    # spec, and pydantic's own message for them is unhelpful.
    if not isinstance(raw, dict):
        raise SpecLoadError(f"{path.name}: expected a mapping at the top level")
    # Every validator in registry.spec runs here. The pydantic error already
    # lists each failing field with its location; the file name is prefixed
    # so a failure in one of nineteen specs is immediately placeable.
    try:
        return SourceSpec.model_validate(raw)
    except ValidationError as exc:
        raise SpecLoadError(f"{path.name}: {exc}") from exc


def load_spec_dir(directory: str | Path) -> list[SourceSpec]:
    """Load every ``.yaml`` spec in a directory, sorted by key.

    Raises ``SpecLoadError`` if the path is not a directory, if any file fails
    to load, or if two files declare the same ``key``.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise SpecLoadError(f"{directory}: not a directory")
    # Only the .yaml extension is picked up; a .yml or a .yaml.bak is ignored,
    # which is a quiet way to park a draft. Files are read in name order so a
    # load error is reported for the same file every time.
    specs = [load_spec(p) for p in sorted(directory.glob("*.yaml"))]
    # The key is what history is stored against, so two files claiming one
    # key would silently share a table. Refuse the whole directory.
    keys = [s.key for s in specs]
    duplicates = {k for k in keys if keys.count(k) > 1}
    if duplicates:
        raise SpecLoadError(f"{directory}: duplicate source keys {sorted(duplicates)}")
    # Sorted by key rather than file name, so callers get a stable order that
    # does not depend on how files happen to be named.
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
        # Keyed by source key. Uniqueness was already enforced by
        # load_spec_dir; a caller building a registry by hand gets last-wins.
        self._by_key = {s.key: s for s in specs}

    @classmethod
    def from_dir(cls, directory: str | Path) -> SpecRegistry:
        """Build a registry from every spec file in a directory. The usual
        entry point; see ``load_spec_dir`` for what it refuses."""
        return cls(load_spec_dir(directory))

    def get(self, key: str) -> SourceSpec | None:
        """The spec for a key, or ``None`` if unknown."""
        return self._by_key.get(key)

    def require(self, key: str) -> SourceSpec:
        """The spec for a key. Raises ``KeyError`` listing every known key when
        the requested one does not exist, which turns a typo into a one-line fix."""
        spec = self._by_key.get(key)
        if spec is None:
            raise KeyError(f"no spec {key!r}; known: {sorted(self._by_key)}")
        return spec

    def candidates_for_filename(self, filename: str) -> list[SourceSpec]:
        """Every spec whose filename patterns hint at this file. Possibly
        several, possibly none; the header-based detector decides."""
        return [s for s in self._by_key.values() if s.matches_filename(filename)]

    def expected_to_deliver(self) -> list[SourceSpec]:
        """Sources whose silence should raise a freshness alert."""
        # Lifecycle ACTIVE only. Discontinued (WeInfuse BuyBill), no-access
        # and planned sources are never alerted on.
        return [s for s in self._by_key.values() if s.is_expected_to_deliver]

    def __len__(self) -> int:
        return len(self._by_key)

    def __iter__(self):
        # Always key order, so iteration is deterministic regardless of how
        # the registry was built.
        return iter(sorted(self._by_key.values(), key=lambda s: s.key))
