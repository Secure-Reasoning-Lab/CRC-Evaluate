"""Seed corpus collection module.

Provides functionality to collect corpus files from experiment output
and store them with metadata for reuse as seed corpus in benchmarks.
"""

from crsbench.benchmark.seed.collector import CollectionResult, CorpusCollector

__all__ = ["CorpusCollector", "CollectionResult"]
