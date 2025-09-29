"""Validation errors and result types."""

from typing import List, Dict, Any, Optional
from enum import Enum
from pydantic import BaseModel


class ValidationSeverity(str, Enum):
    """Severity levels for validation issues."""
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class ValidationIssue(BaseModel):
    """Single validation issue."""

    severity: ValidationSeverity
    code: str
    message: str
    field: Optional[str] = None
    line: Optional[int] = None
    context: Optional[Dict[str, Any]] = None

    def __str__(self) -> str:
        prefix = f"[{self.severity.upper()}]"
        location = f" ({self.field})" if self.field else ""
        line_info = f" at line {self.line}" if self.line else ""
        return f"{prefix}{location}{line_info}: {self.message}"


class ValidationResult(BaseModel):
    """Complete validation result."""

    is_valid: bool
    issues: List[ValidationIssue] = []
    metadata: Dict[str, Any] = {}

    @property
    def errors(self) -> List[ValidationIssue]:
        """Get only error-level issues."""
        return [issue for issue in self.issues if issue.severity == ValidationSeverity.ERROR]

    @property
    def warnings(self) -> List[ValidationIssue]:
        """Get only warning-level issues."""
        return [issue for issue in self.issues if issue.severity == ValidationSeverity.WARNING]

    @property
    def error_count(self) -> int:
        """Number of errors."""
        return len(self.errors)

    @property
    def warning_count(self) -> int:
        """Number of warnings."""
        return len(self.warnings)

    def add_error(self, code: str, message: str, field: Optional[str] = None,
                  line: Optional[int] = None, context: Optional[Dict[str, Any]] = None):
        """Add an error to the validation result."""
        self.issues.append(ValidationIssue(
            severity=ValidationSeverity.ERROR,
            code=code,
            message=message,
            field=field,
            line=line,
            context=context
        ))
        self.is_valid = False

    def add_warning(self, code: str, message: str, field: Optional[str] = None,
                    line: Optional[int] = None, context: Optional[Dict[str, Any]] = None):
        """Add a warning to the validation result."""
        self.issues.append(ValidationIssue(
            severity=ValidationSeverity.WARNING,
            code=code,
            message=message,
            field=field,
            line=line,
            context=context
        ))

    def add_info(self, code: str, message: str, field: Optional[str] = None,
                 line: Optional[int] = None, context: Optional[Dict[str, Any]] = None):
        """Add an info message to the validation result."""
        self.issues.append(ValidationIssue(
            severity=ValidationSeverity.INFO,
            code=code,
            message=message,
            field=field,
            line=line,
            context=context
        ))

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "is_valid": self.is_valid,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "issues": [
                {
                    "severity": issue.severity,
                    "code": issue.code,
                    "message": issue.message,
                    "field": issue.field,
                    "line": issue.line,
                    "context": issue.context
                }
                for issue in self.issues
            ],
            "metadata": self.metadata
        }

    def summary(self) -> str:
        """Get a human-readable summary."""
        if self.is_valid:
            status = "✅ VALID"
        else:
            status = "❌ INVALID"

        counts = []
        if self.error_count > 0:
            counts.append(f"{self.error_count} error(s)")
        if self.warning_count > 0:
            counts.append(f"{self.warning_count} warning(s)")

        if counts:
            return f"{status} - {', '.join(counts)}"
        else:
            return status


class ValidationError(Exception):
    """Exception raised during validation process (not validation failures)."""

    def __init__(self, message: str, code: Optional[str] = None, context: Optional[Dict[str, Any]] = None):
        self.message = message
        self.code = code or "VALIDATION_ERROR"
        self.context = context or {}
        super().__init__(self.message)


class ValidationWarning(UserWarning):
    """Warning raised during validation process."""

    def __init__(self, message: str, code: Optional[str] = None, context: Optional[Dict[str, Any]] = None):
        self.message = message
        self.code = code or "VALIDATION_WARNING"
        self.context = context or {}
        super().__init__(self.message)


# Common validation error codes
class ValidationCodes:
    """Standard validation error codes."""

    # File-level errors
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    FILE_NOT_READABLE = "FILE_NOT_READABLE"
    YAML_SYNTAX_ERROR = "YAML_SYNTAX_ERROR"
    EMPTY_FILE = "EMPTY_FILE"

    # Schema-level errors
    SCHEMA_VALIDATION_ERROR = "SCHEMA_VALIDATION_ERROR"
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    INVALID_FIELD_TYPE = "INVALID_FIELD_TYPE"
    INVALID_FIELD_VALUE = "INVALID_FIELD_VALUE"

    # Configuration-specific errors
    NO_EVALUATION_MODE = "NO_EVALUATION_MODE"
    NO_HARNESS_FILES = "NO_HARNESS_FILES"
    DUPLICATE_HARNESS_NAME = "DUPLICATE_HARNESS_NAME"
    INVALID_COMMIT_HASH = "INVALID_COMMIT_HASH"
    INVALID_PATH_FORMAT = "INVALID_PATH_FORMAT"
    EMPTY_POV_LIST = "EMPTY_POV_LIST"

    # Warnings
    NO_PATCH_EXCLUSIONS = "NO_PATCH_EXCLUSIONS"
    MANY_HARNESSES = "MANY_HARNESSES"
    COMPLEX_PATHS = "COMPLEX_PATHS"