"""The delivery-semantics fields of ``SourceSpec``: decisions D1 to D6.

``event_date``, ``coverage``, ``coverage_column``, ``vanish``,
``vanish_scope``, ``row_hash_exclude`` and ``as_of_source`` describe how
deliveries of one source relate in time. The model rejects combinations that
would make the curated layer lie - a text event date, a vanish rule with no
way to know what a delivery covers, a control total left out of the hash -
and derives the handful of properties the curation code reads.
"""

from __future__ import annotations

import pytest

from sbh_data_bank.registry.spec import (
    EVENT_DATE_AS_OF,
    AsOfSource,
    CoverageSource,
    SourceSpec,
)


def _spec(**overrides) -> SourceSpec:
    """A small keyed spec with a date, an account, a money column and a
    reference price; each test changes one thing."""
    base = {
        "key": "t.source",
        "name": "n",
        "vendor": "v",
        "natural_key": ["id"],
        "variants": [
            {
                "name": "wide",
                "detect": {"require_all": ["ID"]},
                "columns": [
                    {"target": "id", "sources": ["ID"]},
                    {
                        "target": "event",
                        "sources": ["Event"],
                        "type": "date",
                        "date_formats": ["%Y-%m-%d"],
                    },
                    {"target": "account", "sources": ["Account"]},
                    {
                        "target": "amount",
                        "sources": ["Amount"],
                        "type": "currency",
                        "control_total": True,
                    },
                    {"target": "price_ref", "sources": ["Price"], "type": "currency"},
                    {"target": "ssn", "sources": ["SSN"], "pii": "prohibited"},
                ],
            }
        ],
    }
    return SourceSpec.model_validate({**base, **overrides})


class TestEventDate:
    def test_undeclared_by_default_and_then_nothing_vanishes(self):
        spec = _spec()
        assert spec.event_date is None
        assert spec.event_date_column is None
        assert spec.coverage_target is None
        assert spec.vanishes is False

    def test_a_declared_event_date_is_the_coverage_target(self):
        spec = _spec(event_date="event")
        assert spec.event_date_column == "event"
        assert spec.coverage_target == "event"
        assert spec.vanishes is True

    def test_must_be_a_column(self):
        with pytest.raises(ValueError, match="event_date 'nope' is not a column"):
            _spec(event_date="nope")

    def test_must_be_typed_date(self):
        """A text event date would compare as strings; refused at load."""
        with pytest.raises(ValueError, match="typed text.*must be a date"):
            _spec(event_date="account")

    def test_a_snapshot_has_no_event_date_column(self):
        spec = _spec(event_date=EVENT_DATE_AS_OF, coverage="as_of")
        assert spec.is_snapshot
        assert spec.event_date_column is None
        assert spec.coverage_target is None
        assert spec.vanishes is True

    def test_a_snapshot_must_cover_everything_up_to_its_as_of(self):
        with pytest.raises(ValueError, match="coverage must be 'as_of'"):
            _spec(event_date=EVENT_DATE_AS_OF)


class TestCoverage:
    def test_defaults_to_the_filename_window(self):
        assert _spec().coverage is CoverageSource.FILENAME

    def test_a_coverage_column_overrides_the_target(self):
        """Wellpartner filters on processed date while the event is the
        dispensed date; the window is compared against the filter column."""
        spec = _spec(
            event_date="event",
            coverage_column="processed",
            variants=[
                {
                    "name": "v",
                    "detect": {"require_all": ["ID"]},
                    "columns": [
                        {"target": "id", "sources": ["ID"]},
                        {
                            "target": "event",
                            "sources": ["E"],
                            "type": "date",
                            "date_formats": ["%Y-%m-%d"],
                        },
                        {
                            "target": "processed",
                            "sources": ["P"],
                            "type": "date",
                            "date_formats": ["%Y-%m-%d"],
                        },
                    ],
                }
            ],
        )
        assert spec.coverage_target == "processed"

    def test_a_coverage_column_needs_an_event_date(self):
        with pytest.raises(ValueError, match="coverage_column needs an event_date"):
            _spec(coverage_column="event")

    def test_a_coverage_column_must_be_a_date(self):
        with pytest.raises(ValueError, match="coverage_column 'account' is typed text"):
            _spec(event_date="event", coverage_column="account")


class TestVanish:
    def test_explicit_off_wins_over_the_event_date(self):
        """Patients: has a date, must never retire rows."""
        assert _spec(event_date="event", vanish=False).vanishes is False

    def test_on_without_an_event_date_is_refused(self):
        with pytest.raises(ValueError, match="vanish is true but no event_date"):
            _spec(vanish=True)

    def test_scope_without_vanish_is_refused(self):
        with pytest.raises(ValueError, match="never vanishes"):
            _spec(vanish_scope=["account"])

    def test_scope_columns_must_exist(self):
        with pytest.raises(ValueError, match="vanish_scope column 'site'"):
            _spec(event_date="event", vanish_scope=["site"])

    def test_scope_is_kept_in_order(self):
        spec = _spec(event_date="event", vanish_scope=["account", "id"])
        assert spec.vanish_scope == ["account", "id"]


class TestRowHash:
    def test_hashes_every_landed_column_by_default(self):
        """Prohibited columns are not landed, so they are never hashed."""
        assert _spec().row_hash_columns == ("id", "event", "account", "amount", "price_ref")

    def test_an_excluded_column_leaves_the_hash_but_stays_landed(self):
        spec = _spec(row_hash_exclude=["price_ref"])
        assert "price_ref" in spec.landed_targets
        assert spec.row_hash_columns == ("id", "event", "account", "amount")

    @pytest.mark.parametrize(
        ("target", "message"),
        [
            ("id", "is a key, event-date or coverage column"),
            ("event", "is a key, event-date or coverage column"),
            ("amount", "a control total"),
            ("ssn", "prohibited"),
            ("nope", "no loading variant declares"),
        ],
    )
    def test_protected_columns_cannot_be_excluded(self, target, message):
        with pytest.raises(ValueError, match=message):
            _spec(event_date="event", row_hash_exclude=[target])

    def test_the_hash_is_the_union_across_variants(self):
        """D6: a column one variant lacks encodes as null on its rows rather
        than changing which columns are hashed."""
        spec = _spec(
            variants=[
                {
                    "name": "legacy",
                    "detect": {"require_all": ["ID"], "require_none": ["Extra"]},
                    "columns": [
                        {"target": "id", "sources": ["ID"]},
                        {"target": "b", "sources": ["B"]},
                    ],
                },
                {
                    "name": "new_module",
                    "detect": {"require_all": ["ID", "Extra"]},
                    "columns": [
                        {"target": "id", "sources": ["ID"]},
                        {"target": "extra", "sources": ["Extra"]},
                        {"target": "b", "sources": ["B"]},
                    ],
                },
            ]
        )
        # Widest variant's order first, then anything only the other has.
        assert spec.landed_targets == ("id", "extra", "b")
        assert spec.row_hash_columns == spec.landed_targets

    def test_a_recognise_only_variant_does_not_shape_the_hash(self):
        spec = _spec(
            variants=[
                {
                    "name": "detail",
                    "detect": {"require_all": ["ID", "NDC"]},
                    "columns": [
                        {"target": "id", "sources": ["ID"]},
                        {"target": "ndc", "sources": ["NDC"]},
                    ],
                },
                {
                    "name": "summary",
                    "detect": {"require_all": ["ID"], "require_none": ["NDC"]},
                    "action": "recognise_only",
                    "superseded_by": "detail",
                    "columns": [
                        {"target": "id", "sources": ["ID"]},
                        {"target": "summary_only", "sources": ["S"]},
                    ],
                },
            ]
        )
        assert spec.landed_targets == ("id", "ndc")


class TestAsOfSource:
    def test_absent_by_default(self):
        assert _spec().as_of_source is None

    def test_accepts_the_known_sources(self):
        assert _spec(as_of_source="filename").as_of_source is AsOfSource.FILENAME

    def test_rejects_an_unknown_source(self):
        with pytest.raises(ValueError):
            _spec(as_of_source="mtime")
