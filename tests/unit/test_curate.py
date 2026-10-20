"""``curate.rule``: the one dedup rule, outcome by outcome.

The table in CLAUDE.md ("Duplicates") is the contract; each test is one row
of it, plus the vanish outcome as decided in D1 to D3: a current row is
retired only when a newer delivery covers its date, carries its scope
(account), and no longer carries its identity. Facts are built by hand so
the rule is tested on its own; ``facts_for`` is tested once on typed rows.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sbh_data_bank.curate.delivery import Coverage
from sbh_data_bank.curate.rule import DeliveryFacts, Ledger, RowFact, facts_for
from sbh_data_bank.ingest.reader import TypedRow
from sbh_data_bank.registry.spec import CoverageSource, SourceSpec

D1, D2, D3 = date(2026, 2, 4), date(2026, 3, 6), date(2026, 4, 5)
MARCH = Coverage(date(2026, 3, 1), date(2026, 4, 5), CoverageSource.FILENAME)


def _fact(identity: str, hash_: str = "h1", *, occ: int = 1, day=date(2026, 3, 10), acct="A"):
    return RowFact(
        line_number=1,
        identity_hash=identity,
        occurrence=occ,
        row_hash=hash_,
        event_date=day,
        scope=(acct,),
        controls=(Decimal("10"),),
    )


def _delivery(label, as_of, *facts, coverage=MARCH):
    return DeliveryFacts(label=label, as_of=as_of, coverage=coverage, rows=list(facts))


def _ledger() -> Ledger:
    return Ledger(vanish=True, scoped=True)


class TestOutcomes:
    def test_a_new_identity_is_inserted(self):
        out = _ledger().apply(_delivery("d1", D1, _fact("x")))
        assert (out.inserted, out.rows) == (1, 1)

    def test_a_newer_delivery_with_the_same_hash_changes_nothing(self):
        ledger = _ledger()
        ledger.apply(_delivery("d1", D1, _fact("x")))
        out = ledger.apply(_delivery("d2", D2, _fact("x")))
        assert out.unchanged == 1
        assert ledger.current[("x", 1)].delivery == "d1"
        assert ledger.history == []

    def test_a_newer_delivery_with_a_different_hash_corrects(self):
        ledger = _ledger()
        ledger.apply(_delivery("d1", D1, _fact("x", "h1")))
        out = ledger.apply(_delivery("d2", D2, _fact("x", "h2")))
        assert out.corrected == 1
        current = ledger.current[("x", 1)]
        assert (current.row_hash, current.delivery) == ("h2", "d2")
        (gone,) = ledger.history
        assert (gone.reason, gone.by, gone.row.row_hash) == ("corrected", "d2", "h1")

    def test_an_older_delivery_is_late_and_never_current(self):
        ledger = _ledger()
        ledger.apply(_delivery("d2", D2, _fact("x", "h2")))
        out = ledger.apply(_delivery("d1", D1, _fact("x", "h1")))
        assert out.late == 1
        assert ledger.current[("x", 1)].row_hash == "h2"
        (late,) = ledger.history
        assert (late.reason, late.row.row_hash, late.row.delivery) == ("late", "h1", "d1")

    def test_a_same_as_of_different_hash_holds_the_whole_delivery(self):
        """A conflict is a person's decision. Nothing in the batch applies,
        not even the rows that would have been clean inserts."""
        ledger = _ledger()
        ledger.apply(_delivery("d2", D2, _fact("x", "h1")))
        out = ledger.apply(_delivery("d2b", D2, _fact("x", "h2"), _fact("y")))
        assert (out.conflict, out.inserted, out.held) == (1, 1, True)
        assert set(ledger.current) == {("x", 1)}
        assert ledger.current[("x", 1)].row_hash == "h1"

    def test_a_same_as_of_same_hash_is_unchanged(self):
        ledger = _ledger()
        ledger.apply(_delivery("d2", D2, _fact("x")))
        out = ledger.apply(_delivery("d2b", D2, _fact("x")))
        assert (out.unchanged, out.held) == (1, False)

    def test_occurrences_are_separate_identities(self):
        """The McKesson split-shipment lines: same key, occurrence 1 and 2,
        both current."""
        out = _ledger().apply(_delivery("d1", D1, _fact("x", "a"), _fact("x", "b", occ=2)))
        assert out.inserted == 2

    def test_a_swap_of_colliding_lines_is_counted_as_a_reorder(self):
        ledger = _ledger()
        ledger.apply(_delivery("d1", D1, _fact("x", "a"), _fact("x", "b", occ=2)))
        out = ledger.apply(_delivery("d2", D2, _fact("x", "b"), _fact("x", "a", occ=2)))
        assert (out.corrected, out.reordered) == (2, 2)

    def test_replaying_the_oldest_delivery_last_moves_nothing(self):
        """The late-file test from the layer walk, as a unit test."""
        ledger = _ledger()
        ledger.apply(_delivery("d1", D1, _fact("x", "h1"), _fact("y", "k1")))
        ledger.apply(_delivery("d2", D2, _fact("x", "h2"), _fact("y", "k1")))
        before = ledger.snapshot()
        out = ledger.apply(_delivery("d1 again", D1, _fact("x", "h1"), _fact("y", "k1")))
        assert (out.late, out.unchanged) == (1, 1)
        assert ledger.snapshot() == before


class TestVanish:
    def test_a_covered_absent_row_is_superseded_as_vanished(self):
        ledger = _ledger()
        ledger.apply(_delivery("d2", D2, _fact("x"), _fact("y")))
        out = ledger.apply(_delivery("d3", D3, _fact("x")))
        assert out.vanished == 1
        assert set(ledger.current) == {("x", 1)}
        (gone,) = ledger.history
        assert (gone.identity_hash, gone.reason, gone.by) == ("y", "vanished", "d3")

    def test_another_accounts_rows_are_out_of_scope(self):
        """McKesson's June extract carries two accounts the April one does
        not; a delivery without an account says nothing about that account."""
        ledger = _ledger()
        ledger.apply(_delivery("d2", D2, _fact("x", acct="A"), _fact("y", acct="B")))
        out = ledger.apply(_delivery("d3", D3, _fact("x", acct="A")))
        assert out.vanished == 0
        assert set(ledger.current) == {("x", 1), ("y", 1)}

    def test_unscoped_a_delivery_speaks_for_every_account(self):
        ledger = Ledger(vanish=True, scoped=False)
        ledger.apply(_delivery("d2", D2, _fact("x", acct="A"), _fact("y", acct="B")))
        out = ledger.apply(_delivery("d3", D3, _fact("x", acct="A")))
        assert out.vanished == 1

    def test_a_row_outside_the_window_is_kept(self):
        ledger = _ledger()
        ledger.apply(_delivery("d2", D2, _fact("x"), _fact("old", day=date(2025, 6, 1))))
        out = ledger.apply(_delivery("d3", D3, _fact("x")))
        assert out.vanished == 0

    def test_a_delivery_never_vanishes_rows_as_new_as_itself(self):
        ledger = _ledger()
        ledger.apply(_delivery("d3 accounts A", D3, _fact("x")))
        out = ledger.apply(_delivery("d3 accounts B", D3, _fact("z")))
        assert out.vanished == 0

    def test_a_late_delivery_vanishes_nothing(self):
        ledger = _ledger()
        ledger.apply(_delivery("d3", D3, _fact("x"), _fact("y")))
        out = ledger.apply(_delivery("d2", D2, _fact("x")))
        assert (out.late, out.vanished) == (1, 0)

    def test_without_coverage_nothing_vanishes(self):
        ledger = _ledger()
        ledger.apply(_delivery("d2", D2, _fact("x"), _fact("y")))
        out = ledger.apply(_delivery("d3", D3, _fact("x"), coverage=None))
        assert out.vanished == 0

    def test_vanish_off_keeps_every_row(self):
        ledger = Ledger(vanish=False, scoped=True)
        ledger.apply(_delivery("d2", D2, _fact("x"), _fact("y")))
        out = ledger.apply(_delivery("d3", D3, _fact("x")))
        assert out.vanished == 0

    def test_a_held_delivery_vanishes_nothing(self):
        ledger = _ledger()
        ledger.apply(_delivery("d3", D3, _fact("x", "h1"), _fact("y")))
        out = ledger.apply(_delivery("d3b", D3, _fact("x", "h2")))
        assert (out.held, out.vanished) == (True, 0)

    def test_an_undated_row_is_never_vanished(self):
        ledger = _ledger()
        ledger.apply(_delivery("d2", D2, _fact("x"), _fact("nodate", day=None)))
        out = ledger.apply(_delivery("d3", D3, _fact("x")))
        assert out.vanished == 0


def _spec(**overrides) -> SourceSpec:
    base = {
        "key": "t.s",
        "name": "n",
        "vendor": "v",
        "natural_key": ["invoice", "item"],
        "event_date": "invoice_date",
        "vanish_scope": ["account"],
        "variants": [
            {
                "name": "v",
                "detect": {"require_all": ["Invoice"]},
                "columns": [
                    {"target": "invoice", "sources": ["Invoice"]},
                    {"target": "item", "sources": ["Item"]},
                    {"target": "account", "sources": ["Account"]},
                    {
                        "target": "invoice_date",
                        "sources": ["Date"],
                        "type": "date",
                        "date_formats": ["%Y-%m-%d"],
                    },
                    {
                        "target": "amount",
                        "sources": ["Amount"],
                        "type": "currency",
                        "control_total": True,
                    },
                ],
            }
        ],
    }
    return SourceSpec.model_validate({**base, **overrides})


def _row(line, invoice, item, account, day, amount, hash_):
    return TypedRow(
        line_number=line,
        values={
            "invoice": invoice,
            "item": item,
            "account": account,
            "invoice_date": day,
            "amount": Decimal(amount),
            "_row_hash": hash_,
        },
    )


class TestFactsFor:
    def test_identity_occurrence_event_date_scope_and_controls(self):
        spec = _spec()
        rows = [
            _row(2, "I1", "A", "acct1", date(2026, 3, 1), "8", "h-a"),
            _row(3, "I1", "B", "acct1", date(2026, 3, 1), "3", "h-b"),
            _row(4, "I1", "A", "acct1", date(2026, 3, 1), "5", "h-c"),  # split shipment
        ]
        facts = facts_for(spec, rows, as_of=D2, controls=["amount"])
        assert [f.occurrence for f in facts] == [1, 1, 2]
        assert facts[0].identity_hash == facts[2].identity_hash != facts[1].identity_hash
        assert facts[0].event_date == date(2026, 3, 1)
        assert facts[0].scope == ("acct1",)
        assert facts[0].controls == (Decimal("8"),)
        assert [f.row_hash for f in facts] == ["h-a", "h-b", "h-c"]

    def test_a_snapshot_row_takes_the_delivery_as_of_as_its_event_date(self):
        spec = _spec(event_date="as_of", coverage="as_of", vanish_scope=[])
        (fact,) = facts_for(spec, [_row(2, "I1", "A", "x", None, "1", "h")], as_of=D2)
        assert fact.event_date == D2
        assert fact.scope == ()

    def test_a_snapshot_is_replaced_whole_by_a_newer_one(self):
        """Eligibility, wholesaler accounts: everything the old snapshot had
        and the new one lacks is vanished."""
        spec = _spec(event_date="as_of", coverage="as_of", vanish_scope=[])
        ledger = Ledger.for_spec(spec)
        old = facts_for(spec, [_row(2, "I1", "A", "x", None, "1", "h")], as_of=D2)
        new = facts_for(spec, [_row(2, "I2", "A", "x", None, "1", "h")], as_of=D3)
        ledger.apply(DeliveryFacts("old", D2, Coverage(None, D2, CoverageSource.AS_OF), old))
        out = ledger.apply(DeliveryFacts("new", D3, Coverage(None, D3, CoverageSource.AS_OF), new))
        assert (out.inserted, out.vanished) == (1, 1)

    def test_control_sums_run_over_current_rows_only(self):
        spec = _spec()
        ledger = Ledger.for_spec(spec)
        rows = [_row(2, "I1", "A", "a", date(2026, 3, 1), "8", "h1")]
        ledger.apply(
            DeliveryFacts("d2", D2, MARCH, facts_for(spec, rows, as_of=D2, controls=["amount"]))
        )
        rows = [_row(2, "I1", "A", "a", date(2026, 3, 1), "5", "h2")]
        ledger.apply(
            DeliveryFacts("d3", D3, MARCH, facts_for(spec, rows, as_of=D3, controls=["amount"]))
        )
        assert ledger.control_sums(1) == [Decimal("5")]
