"""CRS adapter module.

Provides the unified OssCrsAdapter for crs-compose based CRS evaluation
and a create_adapter() helper for constructing adapter instances.
"""

from crsbench.evaluation.adapter.oss_crs import OssCrsAdapter, create_adapter

__all__ = ["OssCrsAdapter", "create_adapter"]
