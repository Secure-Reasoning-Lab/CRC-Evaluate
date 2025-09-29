"""Patch testing module for CRSBench.

This module provides functionality to apply patches reported by CRS systems
and verify that the patches correctly fix vulnerabilities by ensuring POVs
no longer trigger sanitizers or crashes in the patched codebase.
"""

from crsbench.patch_tester.tester import PatchTester, PatchTestResult, PatchStatus
from crsbench.patch_tester.applicator import PatchApplicator, PatchApplication, ApplicationStatus
from crsbench.patch_tester.validator import PatchValidator, ValidationOutcome
from crsbench.patch_tester.integration import test_crs_patches, create_patch_test_report, CRSPatch, export_patch_test_results
from crsbench.patch_tester.git_manager import GitManager, GitOperation

__all__ = [
    'PatchTester',
    'PatchTestResult',
    'PatchStatus',
    'PatchApplicator',
    'PatchApplication',
    'ApplicationStatus',
    'PatchValidator',
    'ValidationOutcome',
    'test_crs_patches',
    'create_patch_test_report',
    'export_patch_test_results',
    'CRSPatch',
    'GitManager',
    'GitOperation'
]