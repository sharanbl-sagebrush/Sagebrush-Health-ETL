"""The typed-to-curated step: delivery facts and the dedup rule, in memory.

``delivery``  what is known about a delivery before any row is compared:
              its as-of date and where that came from, the date window it
              covers, and the check that it was not extracted before its
              own data.
``rule``      the one dedup rule for every source (``store.schema.DEDUP_RULE``)
              applied delivery by delivery: identity and occurrence,
              inserted / unchanged / corrected / late / conflict / vanished.

Nothing here touches a database. The commit engine will persist what these
modules decide; until then ``scripts/layer_walk.py`` runs them over the real
files and reports the counts.
"""
