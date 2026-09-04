"""NDC normalization against real vendor ground truth.

The McKesson Custom export carries ``10 Digit NDC (Current)`` and
``NDC (Current)`` on the same row, so the vendor tells us how each 10-digit
form is meant to expand.  That makes it a free correctness oracle for the
directory-resolution path, and a permanent guard against anyone reintroducing
a left-pad.

The fixture holds identifiers only - no PHI.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from recon340.transforms.ndc import ResolutionMethod, SetNdcDirectory, normalize_ndc

FIXTURE = Path(__file__).parent.parent / "fixtures" / "ndc_ground_truth_mckesson.json"


@pytest.fixture(scope="module")
def pairs() -> list[tuple[str, str]]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [(ten, eleven) for ten, eleven in data["pairs"]]


@pytest.fixture(scope="module")
def directory(pairs) -> SetNdcDirectory:
    """The NDC dimension, standing in for the FDA directory."""
    return SetNdcDirectory({eleven for _, eleven in pairs})


def _old_zfill(value: str) -> str:
    return re.sub(r"[^0-9]", "", str(value)).zfill(11)


def test_fixture_is_present_and_non_trivial(pairs):
    assert len(pairs) >= 100
    assert all(len(ten) == 10 and len(eleven) == 11 for ten, eleven in pairs)


def test_every_ten_digit_ndc_resolves_to_vendor_truth(pairs, directory):
    """The whole point: 101 real NDCs, zero wrong answers."""
    failures = [
        (ten, truth, normalize_ndc(ten, directory))
        for ten, truth in pairs
        if normalize_ndc(ten, directory).ndc11 != truth
    ]
    assert not failures, f"{len(failures)} of {len(pairs)} did not resolve to vendor truth"


def test_all_resolutions_are_directory_unique(pairs, directory):
    """None of these should be ambiguous - if one becomes ambiguous later it
    means two expansions of the same 10-digit NDC both entered the dimension,
    which is worth knowing about."""
    methods = {normalize_ndc(ten, directory).method for ten, _ in pairs}
    assert methods == {ResolutionMethod.DIRECTORY_UNIQUE}


def test_the_old_left_pad_was_wrong_on_most_of_these(pairs):
    """Documents the blast radius of the defect being fixed.

    If this ever drops to zero the fixture has been replaced with 4-4-2-only
    data and the test above has stopped proving anything.
    """
    wrong = [(ten, truth) for ten, truth in pairs if _old_zfill(ten) != truth]
    assert len(wrong) / len(pairs) > 0.5


def test_the_old_left_pad_invented_ndcs_that_do_not_exist(pairs, directory):
    """Worse than being wrong: the corrupted values look like valid NDCs and
    would silently fail to join rather than raising anything."""
    invented = {_old_zfill(ten) for ten, truth in pairs if _old_zfill(ten) != truth}
    assert invented
    assert not any(directory.contains(n) for n in invented)


def test_resolution_is_deterministic(pairs, directory):
    for ten, _ in pairs[:20]:
        assert normalize_ndc(ten, directory) == normalize_ndc(ten, directory)


def test_without_a_directory_none_of_them_resolve(pairs):
    """Proves the directory is load-bearing, not decorative - the platform must
    not fall back to guessing when the dimension is unavailable."""
    assert not any(normalize_ndc(ten).ok for ten, _ in pairs)
