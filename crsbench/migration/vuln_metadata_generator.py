"""
Vulnerability metadata generator for RFC format.

Generates vuln.yaml files with vulnerability metadata, reading from
Team-Atlanta format vulnerability files or generating mock data if unavailable.
"""

from crsbench.utils.logger import get_logger
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from crsbench.migration.models import VulnerabilityLocation, VulnerabilityMetadata


class VulnMetadataGenerator:
    """Generates vulnerability metadata YAML files in RFC format."""

    def __init__(self):
        """Initialize the vulnerability metadata generator."""
        self.logger = get_logger(__name__)

    def generate(
        self,
        cpv: Dict[str, Any],
        harness_name: str,
        source_vuln_file: Optional[Path] = None,
        crash_log_path: Optional[Path] = None,
        language: str = "C"
    ) -> VulnerabilityMetadata:
        """
        Generate vulnerability metadata from CPV information.

        Args:
            cpv: CPV dictionary from Team-Atlanta config.yaml
            harness_name: Name of the harness this vulnerability is associated with
            source_vuln_file: Path to original Team-Atlanta vulnerability YAML file
            crash_log_path: Path to crash log file for extracting function name
            language: Programming language (e.g., "C", "JVM")

        Returns:
            VulnerabilityMetadata object containing vulnerability metadata in RFC format
        """
        cpv_name = cpv.get('name', 'unknown')

        # Parse function name from crash log if available
        function_name = None
        if crash_log_path and crash_log_path.exists():
            function_name = self._parse_function_name_from_crash_log(crash_log_path, language)
            if function_name:
                self.logger.debug(f"Parsed function name '{function_name}' from crash log")
            else:
                self.logger.debug(f"Could not parse function name from crash log: {crash_log_path}")

        # Try to read original vulnerability file if it exists
        if source_vuln_file and source_vuln_file.exists():
            return self._load_from_original(cpv_name, source_vuln_file, function_name)
        else:
            # Generate MOCK data if original file not found
            self.logger.warning(f"Original vulnerability file not found for {cpv_name}, generating MOCK data")
            return self._generate_mock(cpv, harness_name, cpv_name, function_name)

    def _parse_function_name_from_crash_log(self, crash_log_path: Path, language: str) -> Optional[str]:
        """
        Parse function name from crash log by extracting it from the topmost stack trace.

        Args:
            crash_log_path: Path to crash log file
            language: Programming language (e.g., "C", "JVM")

        Returns:
            Function name if found, None otherwise
        """
        try:
            # Use different parsing strategies based on language
            if language.upper() in ['JVM', 'JAVA']:
                return self._parse_java_stack_trace(crash_log_path)
            else:
                return self._parse_c_stack_trace(crash_log_path)
        except Exception as e:
            self.logger.warning(f"Failed to parse crash log {crash_log_path}: {e}")
            return None

    def _parse_c_stack_trace(self, crash_log_path: Path) -> Optional[str]:
        """
        Parse function name from C/C++ AddressSanitizer crash log.

        Looks for pattern: #0 0x<hex> in <function_name> <file>:<line>:<col>

        Args:
            crash_log_path: Path to crash log file

        Returns:
            Function name if found, None otherwise
        """
        with open(crash_log_path, 'r') as f:
            for line in f:
                # Example: #0 0x56470813e7ba in extremelygoodprtcl_sm /src/curl/lib/extremelygoodprtcl.c:306:33
                match = re.match(r'^\s*#0\s+0x[0-9a-f]+\s+in\s+(\w+)', line)
                if match:
                    return match.group(1)
        return None

    def _parse_java_stack_trace(self, crash_log_path: Path) -> Optional[str]:
        """
        Parse method name from Java/JVM crash log.

        Looks for pattern: at [module/]<ClassName>.<methodName>(<FileName>:<line>)

        Args:
            crash_log_path: Path to crash log file

        Returns:
            Method name if found, None otherwise
        """
        with open(crash_log_path, 'r') as f:
            for line in f:
                # Examples:
                # at org.apache.pdfbox.ocr.OCRStreamEngine.doOCR(OCRStreamEngine.java:190)
                # at java.base/java.util.regex.Pattern$BranchConn.match(Pattern.java:4698)
                # Pattern: at [module/]<package.ClassName>.<methodName>(
                match = re.match(r'^\s*at\s+(?:[^\s]+\.)(\w+)\(', line)
                if match:
                    return match.group(1)
        return None

    def _load_from_original(
        self,
        cpv_name: str,
        source_file: Path,
        function_name_from_crash: Optional[str] = None
    ) -> VulnerabilityMetadata:
        """
        Load vulnerability metadata from original Team-Atlanta format file.

        Args:
            cpv_name: CPV identifier
            source_file: Path to original vulnerability YAML file
            function_name_from_crash: Function name parsed from crash log

        Returns:
            VulnerabilityMetadata instance
        """
        with open(source_file, 'r') as f:
            data = yaml.safe_load(f)

        # Extract fields from Team-Atlanta format
        name = data.get('name', f'MOCK: {cpv_name} vulnerability')

        # Extract details section
        details = data.get('details', {})
        cwes = details.get('cwes', [])
        description = details.get('description', '').strip()

        # Extract and convert locations
        locations = []
        for loc in details.get('locations', []):
            # Use function name from crash log if not present in original location
            func_name = loc.get('function_name') or function_name_from_crash

            location = VulnerabilityLocation(
                path_from_root=loc.get('path_from_root', 'MOCK: unknown path'),
                function_name=func_name,
                startLine=loc.get('startLine', 0),
                startColumn=loc.get('startColumn', 0),
                endLine=loc.get('endLine', 0),
                endColumn=loc.get('endColumn', 0)
            )
            locations.append(location)

        # If no locations, add a mock one
        if not locations:
            locations = [
                VulnerabilityLocation(
                    path_from_root="MOCK: path/to/vulnerable/file.c",
                    function_name=function_name_from_crash,
                    startLine=0,
                    startColumn=0,
                    endLine=0,
                    endColumn=0
                )
            ]

        return VulnerabilityMetadata(
            id=cpv_name,
            name=name,
            cwes=cwes,
            description=description if description else f"MOCK: No description provided for {cpv_name}",
            locations=locations
        )

    def _generate_mock(
        self,
        cpv: Dict[str, Any],
        harness_name: str,
        cpv_name: str,
        function_name_from_crash: Optional[str] = None
    ) -> VulnerabilityMetadata:
        """
        Generate mock vulnerability metadata when original file is not available.

        Args:
            cpv: CPV dictionary from config.yaml
            harness_name: Harness name
            cpv_name: CPV identifier
            function_name_from_crash: Function name parsed from crash log

        Returns:
            VulnerabilityMetadata with MOCK data
        """
        sanitizer = cpv.get('sanitizer', 'address')
        error_token = cpv.get('error_token', '')

        return VulnerabilityMetadata(
            id=cpv_name,
            name=f"MOCK: {cpv_name} vulnerability in {harness_name}",
            cwes=[],
            description=self._generate_description(cpv_name, harness_name, sanitizer, error_token),
            locations=[
                VulnerabilityLocation(
                    path_from_root="MOCK: path/to/vulnerable/file.c",
                    function_name=function_name_from_crash,
                    startLine=0,
                    startColumn=0,
                    endLine=0,
                    endColumn=0
                )
            ]
        )

    def _generate_description(self, cpv_name: str, harness_name: str,
                             sanitizer: str, error_token: str) -> str:
        """
        Generate a descriptive vulnerability description.

        Args:
            cpv_name: CPV identifier
            harness_name: Harness name
            sanitizer: Sanitizer type
            error_token: Error token from crash

        Returns:
            Generated description string
        """
        description = (
            f"MOCK: Vulnerability {cpv_name} detected in harness {harness_name}. "
            f"This vulnerability is detected by {sanitizer} sanitizer."
        )

        if error_token and not error_token.startswith('MOCK'):
            description += f" The error signature is: '{error_token}'."

        description += (
            "\n\n"
            "NOTE: This description is auto-generated from limited metadata in the "
            "Team-Atlanta format. For accurate vulnerability details, please analyze "
            "the patches, POV blobs, and crash logs."
        )

        return description

