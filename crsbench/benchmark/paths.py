"""Compatibility shim for benchmark ground-truth path helpers.

New code should import from `crsbench.validation.ground_truth_paths`.
"""

from crsbench.validation.ground_truth_paths import GroundTruthPaths as _GroundTruthPaths

GroundTruthPaths = _GroundTruthPaths
