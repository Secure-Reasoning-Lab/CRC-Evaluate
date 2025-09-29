"""Detection logic for various POV validation indicators."""

import re
import logging
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Pattern
from crsbench.reproducer.harness import ExecutionResult

logger = logging.getLogger(__name__)


class BaseDetector(ABC):
    """Base class for behavior detectors."""

    @abstractmethod
    def detect(self, execution_result: ExecutionResult, *args, **kwargs) -> bool:
        """Detect specific behavior in execution result."""
        pass


class SanitizerDetector(BaseDetector):
    """Detects sanitizer triggers in execution output."""

    def __init__(self):
        """Initialize sanitizer detection patterns."""
        self.sanitizer_patterns: Dict[str, List[Pattern]] = {
            "address": [
                re.compile(r"AddressSanitizer", re.IGNORECASE),
                re.compile(r"heap-buffer-overflow", re.IGNORECASE),
                re.compile(r"stack-buffer-overflow", re.IGNORECASE),
                re.compile(r"heap-use-after-free", re.IGNORECASE),
                re.compile(r"stack-use-after-return", re.IGNORECASE),
                re.compile(r"use-after-poison", re.IGNORECASE),
                re.compile(r"container-overflow", re.IGNORECASE),
                re.compile(r"stack-use-after-scope", re.IGNORECASE),
                re.compile(r"global-buffer-overflow", re.IGNORECASE),
                re.compile(r"initialization-order-fiasco", re.IGNORECASE),
                re.compile(r"memory-leaks", re.IGNORECASE),
            ],
            "memory": [
                re.compile(r"MemorySanitizer", re.IGNORECASE),
                re.compile(r"use-of-uninitialized-value", re.IGNORECASE),
                re.compile(r"uninitialized bytes", re.IGNORECASE),
            ],
            "undefined": [
                re.compile(r"UndefinedBehaviorSanitizer", re.IGNORECASE),
                re.compile(r"runtime error", re.IGNORECASE),
                re.compile(r"signed integer overflow", re.IGNORECASE),
                re.compile(r"division by zero", re.IGNORECASE),
                re.compile(r"shift exponent", re.IGNORECASE),
                re.compile(r"null pointer", re.IGNORECASE),
                re.compile(r"misaligned address", re.IGNORECASE),
            ],
            "thread": [
                re.compile(r"ThreadSanitizer", re.IGNORECASE),
                re.compile(r"data race", re.IGNORECASE),
                re.compile(r"race on", re.IGNORECASE),
            ],
            "leak": [
                re.compile(r"LeakSanitizer", re.IGNORECASE),
                re.compile(r"Direct leak", re.IGNORECASE),
                re.compile(r"Indirect leak", re.IGNORECASE),
                re.compile(r"LEAK SUMMARY", re.IGNORECASE),
            ]
        }

        # Common sanitizer error patterns
        self.common_patterns = [
            re.compile(r"ERROR: \w+Sanitizer", re.IGNORECASE),
            re.compile(r"SUMMARY: \w+Sanitizer", re.IGNORECASE),
        ]

    def detect(self,
               execution_result: ExecutionResult,
               sanitizer_type: str = None,
               error_token: str = None) -> bool:
        """Detect sanitizer triggers in execution output.

        Args:
            execution_result: Result from harness execution
            sanitizer_type: Expected sanitizer type (address, memory, etc.)
            error_token: Specific error token to look for

        Returns:
            True if sanitizer trigger detected, False otherwise
        """
        output = execution_result.stderr + "\n" + execution_result.stdout

        # If specific error token provided, check for it first
        if error_token:
            if error_token.lower() in output.lower():
                logger.debug(f"Found specific error token: {error_token}")
                return True

        # Check for common sanitizer patterns
        for pattern in self.common_patterns:
            if pattern.search(output):
                logger.debug(f"Found common sanitizer pattern: {pattern.pattern}")
                return True

        # Check for specific sanitizer type patterns
        if sanitizer_type and sanitizer_type in self.sanitizer_patterns:
            for pattern in self.sanitizer_patterns[sanitizer_type]:
                if pattern.search(output):
                    logger.debug(f"Found {sanitizer_type} sanitizer pattern: {pattern.pattern}")
                    return True

        # Check all sanitizer patterns if no specific type given
        elif not sanitizer_type:
            for san_type, patterns in self.sanitizer_patterns.items():
                for pattern in patterns:
                    if pattern.search(output):
                        logger.debug(f"Found {san_type} sanitizer pattern: {pattern.pattern}")
                        return True

        return False


class CrashDetector(BaseDetector):
    """Detects crashes and abnormal termination."""

    def __init__(self):
        """Initialize crash detection patterns."""
        self.crash_patterns = [
            re.compile(r"segmentation fault", re.IGNORECASE),
            re.compile(r"segfault", re.IGNORECASE),
            re.compile(r"bus error", re.IGNORECASE),
            re.compile(r"floating point exception", re.IGNORECASE),
            re.compile(r"illegal instruction", re.IGNORECASE),
            re.compile(r"abort.*called", re.IGNORECASE),
            re.compile(r"terminated by signal", re.IGNORECASE),
            re.compile(r"core dumped", re.IGNORECASE),
            re.compile(r"killed by signal", re.IGNORECASE),
        ]

        # Signal-based crash indicators
        self.crash_signals = [
            4,   # SIGILL
            6,   # SIGABRT
            8,   # SIGFPE
            10,  # SIGBUS
            11,  # SIGSEGV
        ]

    def detect(self, execution_result: ExecutionResult) -> bool:
        """Detect crashes in execution result.

        Args:
            execution_result: Result from harness execution

        Returns:
            True if crash detected, False otherwise
        """
        output = execution_result.stderr + "\n" + execution_result.stdout

        # Check for crash patterns in output
        for pattern in self.crash_patterns:
            if pattern.search(output):
                logger.debug(f"Found crash pattern: {pattern.pattern}")
                return True

        # Check return codes that indicate crashes
        if execution_result.return_code in self.crash_signals:
            logger.debug(f"Crash indicated by return code: {execution_result.return_code}")
            return True

        # Check for negative return codes (typically signal-based termination)
        if execution_result.return_code < 0:
            logger.debug(f"Crash indicated by negative return code: {execution_result.return_code}")
            return True

        # Check for signal-based termination (128 + signal)
        if execution_result.return_code > 128:
            signal_num = execution_result.return_code - 128
            if signal_num in self.crash_signals:
                logger.debug(f"Crash indicated by signal {signal_num}")
                return True

        return False


class TimeoutDetector(BaseDetector):
    """Detects timeout conditions."""

    def __init__(self, expected_timeout: int):
        """Initialize timeout detector.

        Args:
            expected_timeout: Expected timeout value in seconds
        """
        self.expected_timeout = expected_timeout
        self.timeout_patterns = [
            re.compile(r"timeout", re.IGNORECASE),
            re.compile(r"killed.*timeout", re.IGNORECASE),
            re.compile(r"execution timed out", re.IGNORECASE),
        ]

    def detect(self, execution_result: ExecutionResult) -> bool:
        """Detect timeout conditions.

        Args:
            execution_result: Result from harness execution

        Returns:
            True if timeout detected, False otherwise
        """
        # Check if execution was explicitly marked as timed out
        if execution_result.timed_out:
            logger.debug("Execution explicitly marked as timed out")
            return True

        # Check if execution time exceeds expected timeout
        if execution_result.execution_time >= self.expected_timeout:
            logger.debug(f"Execution time ({execution_result.execution_time}s) >= timeout ({self.expected_timeout}s)")
            return True

        # Check for timeout patterns in output
        output = execution_result.stderr + "\n" + execution_result.stdout
        for pattern in self.timeout_patterns:
            if pattern.search(output):
                logger.debug(f"Found timeout pattern: {pattern.pattern}")
                return True

        return False


class CustomPatternDetector(BaseDetector):
    """Detects custom patterns in execution output."""

    def __init__(self, patterns: List[str]):
        """Initialize custom pattern detector.

        Args:
            patterns: List of regex patterns to match
        """
        self.patterns = [re.compile(pattern, re.IGNORECASE) for pattern in patterns]

    def detect(self, execution_result: ExecutionResult) -> bool:
        """Detect custom patterns in execution output.

        Args:
            execution_result: Result from harness execution

        Returns:
            True if any pattern matches, False otherwise
        """
        output = execution_result.stderr + "\n" + execution_result.stdout

        for pattern in self.patterns:
            if pattern.search(output):
                logger.debug(f"Found custom pattern: {pattern.pattern}")
                return True

        return False