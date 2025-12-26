"""CRSBench - A benchmark suite for evaluating Cyber Reasoning Systems."""

__version__ = "0.1.0"
__author__ = "CRSBench Team"
__description__ = "A benchmark suite for evaluating Cyber Reasoning Systems"

# Make submodules available (explicit re-exports)
from crsbench import hint_generation as hint_generation
from crsbench import migration as migration
from crsbench import utils as utils
from crsbench import validation as validation
