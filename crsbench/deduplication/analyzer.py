"""Root cause analysis for POV deduplication."""

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional, Any, Set
from enum import Enum


class VulnerabilityType(Enum):
    """Types of vulnerabilities that can be detected."""
    BUFFER_OVERFLOW = "buffer_overflow"
    HEAP_OVERFLOW = "heap_overflow"
    STACK_OVERFLOW = "stack_overflow"
    USE_AFTER_FREE = "use_after_free"
    DOUBLE_FREE = "double_free"
    NULL_POINTER_DEREFERENCE = "null_pointer_dereference"
    MEMORY_LEAK = "memory_leak"
    UNINITIALIZED_MEMORY = "uninitialized_memory"
    INTEGER_OVERFLOW = "integer_overflow"
    FORMAT_STRING = "format_string"
    UNKNOWN = "unknown"


@dataclass
class StackFrame:
    """Represents a single frame in a stack trace."""
    function: str
    file: str
    line: Optional[int] = None
    address: Optional[str] = None


@dataclass
class RootCause:
    """Root cause information for a POV.

    This represents the fundamental cause of a vulnerability,
    used for deduplication purposes.
    """
    vulnerability_type: VulnerabilityType
    source_location: str  # File:line or function where vulnerability originates
    stack_trace: List[StackFrame]
    error_signature: str  # Normalized error pattern

    # Additional metadata for similarity analysis
    affected_variable: Optional[str] = None
    allocation_site: Optional[str] = None
    deallocation_site: Optional[str] = None

    # Confidence score (0.0-1.0) for root cause identification
    confidence: float = 1.0

    def __hash__(self) -> int:
        """Hash based on key identifying properties."""
        return hash((
            self.vulnerability_type,
            self.source_location,
            self.error_signature,
            tuple((f.function, f.file, f.line) for f in self.stack_trace[:3])  # Top 3 frames
        ))

    def __eq__(self, other) -> bool:
        """Equality based on root cause similarity."""
        if not isinstance(other, RootCause):
            return False
        return (
            self.vulnerability_type == other.vulnerability_type and
            self.source_location == other.source_location and
            self.error_signature == other.error_signature
        )


class RootCauseAnalyzer(ABC):
    """Abstract base class for root cause analysis."""

    @abstractmethod
    def analyze_pov(self, pov_output: str, sanitizer: str, error_token: str) -> Optional[RootCause]:
        """Analyze POV output to determine root cause.

        Args:
            pov_output: Raw output from CRS/sanitizer
            sanitizer: Type of sanitizer used (address, memory, etc.)
            error_token: Expected error pattern

        Returns:
            RootCause object if analysis succeeds, None otherwise
        """
        pass


class AddressSanitizerAnalyzer(RootCauseAnalyzer):
    """Root cause analyzer for AddressSanitizer output."""

    def __init__(self):
        self.error_patterns = {
            VulnerabilityType.HEAP_OVERFLOW: [
                r"heap-buffer-overflow",
                r"heap-buffer-overflow on address"
            ],
            VulnerabilityType.STACK_OVERFLOW: [
                r"stack-buffer-overflow",
                r"stack-overflow"
            ],
            VulnerabilityType.USE_AFTER_FREE: [
                r"heap-use-after-free",
                r"use-after-free"
            ],
            VulnerabilityType.DOUBLE_FREE: [
                r"double-free",
                r"attempting double-free"
            ],
            VulnerabilityType.NULL_POINTER_DEREFERENCE: [
                r"SEGV on unknown address",
                r"null-pointer-dereference"
            ]
        }

    def analyze_pov(self, pov_output: str, sanitizer: str, error_token: str) -> Optional[RootCause]:
        """Analyze AddressSanitizer output for root cause."""
        if sanitizer != "address":
            return None

        # Determine vulnerability type from error pattern
        vuln_type = self._classify_vulnerability(pov_output, error_token)

        # Extract stack trace
        stack_trace = self._parse_stack_trace(pov_output)

        # Determine source location (first non-system frame)
        source_location = self._find_source_location(stack_trace)

        # Normalize error signature
        error_signature = self._normalize_error_signature(error_token)

        # Extract additional metadata
        affected_var = self._extract_variable_name(pov_output)
        alloc_site = self._find_allocation_site(pov_output)

        return RootCause(
            vulnerability_type=vuln_type,
            source_location=source_location,
            stack_trace=stack_trace,
            error_signature=error_signature,
            affected_variable=affected_var,
            allocation_site=alloc_site,
            confidence=0.8  # TODO: Implement confidence scoring
        )

    def _classify_vulnerability(self, output: str, error_token: str) -> VulnerabilityType:
        """Classify vulnerability type from sanitizer output."""
        output_lower = output.lower()
        token_lower = error_token.lower()

        for vuln_type, patterns in self.error_patterns.items():
            for pattern in patterns:
                if re.search(pattern, output_lower) or re.search(pattern, token_lower):
                    return vuln_type

        return VulnerabilityType.UNKNOWN

    def _parse_stack_trace(self, output: str) -> List[StackFrame]:
        """Parse stack trace from AddressSanitizer output."""
        frames = []

        # Pattern for AddressSanitizer stack frames
        # Example: "    #0 0x... in function_name file.c:123:45"
        frame_pattern = r'#\d+\s+0x[0-9a-f]+\s+in\s+(\S+)\s+([^:]+):(\d+)(?::\d+)?'

        for match in re.finditer(frame_pattern, output):
            function = match.group(1)
            file = match.group(2)
            line = int(match.group(3)) if match.group(3) else None

            frames.append(StackFrame(
                function=function,
                file=file,
                line=line
            ))

        return frames

    def _find_source_location(self, stack_trace: List[StackFrame]) -> str:
        """Find the primary source location from stack trace."""
        if not stack_trace:
            return "unknown:0"

        # Skip system/library frames, find first user code frame
        for frame in stack_trace:
            if not self._is_system_frame(frame):
                return f"{frame.file}:{frame.line or 0}"

        # Fallback to first frame
        first_frame = stack_trace[0]
        return f"{first_frame.file}:{first_frame.line or 0}"

    def _is_system_frame(self, frame: StackFrame) -> bool:
        """Check if a stack frame is from system/library code."""
        system_indicators = [
            '/usr/', '/lib/', '/lib64/', 'libc.so', 'libstdc++',
            '__libc_', '_start', 'main', '__sanitizer'
        ]

        return any(indicator in frame.file or indicator in frame.function
                  for indicator in system_indicators)

    def _normalize_error_signature(self, error_token: str) -> str:
        """Normalize error signature for consistent comparison."""
        # Remove addresses and variable instance-specific information
        normalized = re.sub(r'0x[0-9a-f]+', 'ADDRESS', error_token)
        normalized = re.sub(r'\d+', 'N', normalized)
        return normalized.lower().strip()

    def _extract_variable_name(self, output: str) -> Optional[str]:
        """Extract affected variable name from output."""
        # Look for patterns like "variable 'var_name'"
        var_match = re.search(r"variable\s+'([^']+)'", output)
        if var_match:
            return var_match.group(1)

        return None

    def _find_allocation_site(self, output: str) -> Optional[str]:
        """Find allocation site from AddressSanitizer output."""
        # Look for allocation stack trace
        alloc_pattern = r'allocated by thread.*?at:\s*([^\n]+)'
        match = re.search(alloc_pattern, output, re.DOTALL)
        if match:
            return match.group(1).strip()

        return None


class MultiSanitizerAnalyzer(RootCauseAnalyzer):
    """Root cause analyzer that supports multiple sanitizers."""

    def __init__(self):
        self.analyzers = {
            "address": AddressSanitizerAnalyzer(),
            # TODO: Add other sanitizer analyzers
            # "memory": MemorySanitizerAnalyzer(),
            # "thread": ThreadSanitizerAnalyzer(),
            # "undefined": UBSanAnalyzer(),
        }

    def analyze_pov(self, pov_output: str, sanitizer: str, error_token: str) -> Optional[RootCause]:
        """Analyze POV using appropriate sanitizer analyzer."""
        analyzer = self.analyzers.get(sanitizer.lower())
        if not analyzer:
            # Fallback: try all analyzers
            for analyzer in self.analyzers.values():
                result = analyzer.analyze_pov(pov_output, sanitizer, error_token)
                if result:
                    return result
            return None

        return analyzer.analyze_pov(pov_output, sanitizer, error_token)