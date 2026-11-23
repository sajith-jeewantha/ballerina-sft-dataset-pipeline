"""Shared building blocks for the Ballerina Q&A dataset pipeline.

Both `scraper/` and `sft-dataset-generator/` depend on this package so that
config, the SQLite store, hashing and logging are defined exactly once.
"""

__version__ = "2.2.0"
