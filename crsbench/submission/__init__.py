"""CRC submission validation and evaluator-local registration."""

from crsbench.submission.manifest import (
    RegisteredSubmission,
    SubmissionError,
    ValidatedSubmission,
    load_submission,
    register_submission,
)

__all__ = [
    "RegisteredSubmission",
    "SubmissionError",
    "ValidatedSubmission",
    "load_submission",
    "register_submission",
]
