"""Review service (PoC2).

The service takes a tar.gz payload from the builder, runs the three writer
subagents through a Claude runner, and publishes a signed bundle. See
``docs/shortcomings.md`` for how this replaces the on-builder pipeline.
"""
