"""Proof of Concept (POC) and Proof of Vulnerability (POV) handling for CRSBench builders.

This module provides classes and utilities for handling POCs and POVs in the builder
system. The design bridges between CRSBench's POV format and external builder systems
like OSS-Fuzz. Parts of the implementation are inspired by PatchAgent's PoC handling
(https://github.com/cla7aye15I4nd/PatchAgent) under Apache 2.0 license.

Original PatchAgent citation:
    Yu, Zheng et al. "PatchAgent: A Practical Program Repair Agent Mimicking Human Expertise"
    34rd USENIX Security Symposium (USENIX Security 25), 2025.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
import hashlib

logger = logging.getLogger(__name__)


class POCType(Enum):
    """Type of Proof of Concept."""
    FILE = "file"           # POC from file
    BINARY = "binary"       # Binary POC data
    STDIN = "stdin"         # POC sent via stdin
    CMDLINE = "cmdline"     # POC via command line arguments
    NETWORK = "network"     # Network-based POC
    OSSFUZZ = "ossfuzz"     # OSS-Fuzz specific POC


@dataclass
class POCMetadata:
    """Metadata for a POC."""
    name: str
    poc_type: POCType
    target_harness: str
    expected_sanitizer: Optional[str] = None
    expected_error_token: Optional[str] = None
    description: Optional[str] = None
    tags: List[str] = None

    def __post_init__(self):
        if self.tags is None:
            self.tags = []


class POC(ABC):
    """Abstract base class for Proof of Concept data.

    This class provides the interface for different types of POCs.
    Implementations should inherit from this class and provide
    specific handling for their POC type.

    The design is inspired by PatchAgent's PoC class but extended
    for CRSBench's requirements.
    """

    def __init__(self, metadata: Optional[POCMetadata] = None):
        """Initialize POC.

        Args:
            metadata: Optional metadata for the POC
        """
        self.metadata = metadata
        self._hash_cache: Optional[str] = None

    @property
    @abstractmethod
    def data(self) -> bytes:
        """Get the POC data as bytes."""
        pass

    @property
    @abstractmethod
    def poc_type(self) -> POCType:
        """Get the type of this POC."""
        pass

    @property
    def name(self) -> str:
        """Get the name of this POC."""
        if self.metadata:
            return self.metadata.name
        return f"poc_{self.hash[:8]}"

    @property
    def target_harness(self) -> Optional[str]:
        """Get the target harness for this POC."""
        if self.metadata:
            return self.metadata.target_harness
        return None

    @property
    def hash(self) -> str:
        """Get a hash of the POC data for caching/identification."""
        if self._hash_cache is None:
            self._hash_cache = hashlib.sha256(self.data).hexdigest()
        return self._hash_cache

    def to_dict(self) -> Dict[str, Any]:
        """Convert POC to dictionary representation."""
        result = {
            "name": self.name,
            "type": self.poc_type.value,
            "hash": self.hash,
            "data_size": len(self.data)
        }

        if self.metadata:
            result.update({
                "target_harness": self.metadata.target_harness,
                "expected_sanitizer": self.metadata.expected_sanitizer,
                "expected_error_token": self.metadata.expected_error_token,
                "description": self.metadata.description,
                "tags": self.metadata.tags
            })

        return result

    def __str__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name}, type={self.poc_type.value})"

    def __repr__(self) -> str:
        return self.__str__()


class FilePOC(POC):
    """POC that reads data from a file."""

    def __init__(self, file_path: Path, metadata: Optional[POCMetadata] = None):
        """Initialize file-based POC.

        Args:
            file_path: Path to POC file
            metadata: Optional metadata
        """
        super().__init__(metadata)
        self.file_path = Path(file_path)

        if not self.file_path.exists():
            raise FileNotFoundError(f"POC file not found: {file_path}")

    @property
    def data(self) -> bytes:
        """Read POC data from file."""
        return self.file_path.read_bytes()

    @property
    def poc_type(self) -> POCType:
        """Get POC type."""
        return POCType.FILE

    @property
    def name(self) -> str:
        """Get POC name, preferring file name if no metadata name."""
        if self.metadata and self.metadata.name:
            return self.metadata.name
        return self.file_path.stem


class BinaryPOC(POC):
    """POC with binary data."""

    def __init__(self, data: bytes, metadata: Optional[POCMetadata] = None):
        """Initialize binary POC.

        Args:
            data: Binary POC data
            metadata: Optional metadata
        """
        super().__init__(metadata)
        self._data = data

    @property
    def data(self) -> bytes:
        """Get POC data."""
        return self._data

    @property
    def poc_type(self) -> POCType:
        """Get POC type."""
        return POCType.BINARY


class StdinPOC(POC):
    """POC that provides input via stdin."""

    def __init__(self, stdin_data: Union[str, bytes], metadata: Optional[POCMetadata] = None):
        """Initialize stdin POC.

        Args:
            stdin_data: Data to send via stdin
            metadata: Optional metadata
        """
        super().__init__(metadata)
        if isinstance(stdin_data, str):
            stdin_data = stdin_data.encode()
        self._data = stdin_data

    @property
    def data(self) -> bytes:
        """Get stdin data."""
        return self._data

    @property
    def poc_type(self) -> POCType:
        """Get POC type."""
        return POCType.STDIN


class CmdlinePOC(POC):
    """POC that provides input via command line arguments."""

    def __init__(self, arguments: List[str], metadata: Optional[POCMetadata] = None):
        """Initialize command line POC.

        Args:
            arguments: Command line arguments
            metadata: Optional metadata
        """
        super().__init__(metadata)
        self.arguments = arguments
        # Serialize arguments as data
        self._data = '\n'.join(arguments).encode()

    @property
    def data(self) -> bytes:
        """Get serialized arguments as data."""
        return self._data

    @property
    def poc_type(self) -> POCType:
        """Get POC type."""
        return POCType.CMDLINE


def create_poc_from_file(file_path: Path, target_harness: str,
                        expected_sanitizer: Optional[str] = None,
                        expected_error_token: Optional[str] = None) -> FilePOC:
    """Create a FilePOC from a file path with metadata.

    Args:
        file_path: Path to POC file
        target_harness: Name of target harness
        expected_sanitizer: Expected sanitizer that should trigger
        expected_error_token: Expected error message/token

    Returns:
        FilePOC instance
    """
    metadata = POCMetadata(
        name=file_path.stem,
        poc_type=POCType.FILE,
        target_harness=target_harness,
        expected_sanitizer=expected_sanitizer,
        expected_error_token=expected_error_token
    )

    return FilePOC(file_path, metadata)


def create_poc_from_data(data: bytes, name: str, target_harness: str,
                        expected_sanitizer: Optional[str] = None,
                        expected_error_token: Optional[str] = None) -> BinaryPOC:
    """Create a BinaryPOC from raw data with metadata.

    Args:
        data: Binary POC data
        name: Name for the POC
        target_harness: Name of target harness
        expected_sanitizer: Expected sanitizer that should trigger
        expected_error_token: Expected error message/token

    Returns:
        BinaryPOC instance
    """
    metadata = POCMetadata(
        name=name,
        poc_type=POCType.BINARY,
        target_harness=target_harness,
        expected_sanitizer=expected_sanitizer,
        expected_error_token=expected_error_token
    )

    return BinaryPOC(data, metadata)


def load_pocs_from_directory(poc_dir: Path, harness_name: Optional[str] = None) -> List[POC]:
    """Load all POCs from a directory.

    Args:
        poc_dir: Directory containing POC files
        harness_name: Optional harness name to associate with POCs

    Returns:
        List of POC instances
    """
    pocs = []

    if not poc_dir.exists() or not poc_dir.is_dir():
        logger.warning(f"POC directory does not exist: {poc_dir}")
        return pocs

    for poc_file in poc_dir.iterdir():
        if poc_file.is_file():
            try:
                poc = create_poc_from_file(
                    poc_file,
                    target_harness=harness_name or "unknown",
                    expected_sanitizer="address"  # Default to AddressSanitizer
                )
                pocs.append(poc)
                logger.debug(f"Loaded POC: {poc.name}")
            except Exception as e:
                logger.warning(f"Failed to load POC from {poc_file}: {e}")

    logger.info(f"Loaded {len(pocs)} POCs from {poc_dir}")
    return pocs


def convert_crsbench_pov_to_poc(pov_dict: Dict[str, Any], harness_name: str) -> Optional[POC]:
    """Convert CRSBench POV format to POC instance.

    Args:
        pov_dict: POV dictionary from CRSBench validation schema
        harness_name: Target harness name

    Returns:
        POC instance or None if conversion fails
    """
    try:
        pov_name = pov_dict.get("name", "unknown")
        sanitizer = pov_dict.get("sanitizer", "address")
        error_token = pov_dict.get("error_token")

        # Check if POV has file reference
        if "file" in pov_dict:
            file_path = Path(pov_dict["file"])
            return create_poc_from_file(
                file_path,
                target_harness=harness_name,
                expected_sanitizer=sanitizer,
                expected_error_token=error_token
            )

        # Check if POV has inline data
        elif "data" in pov_dict:
            data = pov_dict["data"]
            if isinstance(data, str):
                data = data.encode()

            return create_poc_from_data(
                data,
                name=pov_name,
                target_harness=harness_name,
                expected_sanitizer=sanitizer,
                expected_error_token=error_token
            )

        else:
            logger.warning(f"POV {pov_name} has no file or data field")
            return None

    except Exception as e:
        logger.error(f"Failed to convert POV to POC: {e}")
        return None