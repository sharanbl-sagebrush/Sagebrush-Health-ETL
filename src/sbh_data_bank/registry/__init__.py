"""Source specs: how one vendor file becomes rows.

Read in this order. ``spec`` is the data model a YAML spec is validated
against; ``loader`` reads the YAML files into that model; ``headers`` matches a
file's header row to a spec's accepted column names; ``binding`` picks the
variant a file is and wires every column to a position, reporting drift.
"""
