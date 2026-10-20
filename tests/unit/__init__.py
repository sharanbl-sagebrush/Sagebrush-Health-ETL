"""Unit tests. Nothing in here needs a database, network access or the real vendor extracts.

Which file covers which production module:

- test_identity.py          core/identity.py      row, file and batch identity (hashing, ids)
- test_temporal.py          core/temporal.py      effective dating: DateRange and TemporalSet
- test_ndc.py               transforms/ndc.py     NDC normalization rules vs the old left-pad
- test_ndc_ground_truth.py  transforms/ndc.py     the same rules against 101 real McKesson NDC pairs
- test_values.py            transforms/values.py  typed parsing of money, dates, text, ints, bools
- test_headers.py           registry/headers.py   normalized header matching and drift detection
- test_binding.py           registry/binding.py, registry/loader.py, registry/spec.py
                                                  spec loading, variant selection, column binding
- test_load.py              ingest/load.py, ingest/reader.py
                                                  reading a file end to end into typed rows
- test_specs_shipped.py     specs/*.yaml          every shipped spec against its real header rows
"""
