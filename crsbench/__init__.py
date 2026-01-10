"""CRSBench - A benchmark suite for evaluating Cyber Reasoning Systems."""

# Get version from setuptools-scm
try:
    from crsbench._version import __version__
except ImportError:
    # Fallback for development without install
    __version__ = "0.1.0.dev0+unknown"

__author__ = "CRSBench Team"
__description__ = "A benchmark suite for evaluating Cyber Reasoning Systems"

# Make submodules available (explicit re-exports)
from crsbench import hint_generation as hint_generation
from crsbench import migration as migration
from crsbench import utils as utils
from crsbench import validation as validation

# Build info
from crsbench._build_info import BuildInfo as BuildInfo
from crsbench._build_info import get_cached_build_info as get_cached_build_info


def get_version_info() -> dict:
    """Get version and build information.

    Returns:
        Dictionary containing version and git commit info for all repos
    """
    build_info = get_cached_build_info()
    return build_info.to_dict()
