"""Storage abstraction with transparent Qdrant→SQLite failover and resync.

Public API mirrors database.py (drop-in: `import storage as db`). When Qdrant is
unreachable/degraded, writes land in a local SQLite mirror (dirty-marked) and
reads are served locally; on recovery a resync uploads the dirty rows back.

Backends are swappable via the FailoverManager (see specs/storage_spec_part1/2).
"""

# STUB_PART_1: Implement the preamble + sections 1-6 (re-exports, SqliteBackend,
# QdrantBackend, FailoverManager, async wrappers, singletons) per
# specs/storage_spec_part1.md, replacing this comment line.

# STUB_PART_2: Implement sections 7-9 (resync, health monitor, entrypoints) per
# specs/storage_spec_part2.md, replacing this comment line.
