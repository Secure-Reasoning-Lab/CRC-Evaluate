"""Pytest configuration and fixtures."""

from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Load .env file from project root if it exists
env_file = Path(__file__).parent.parent / ".env"
if env_file.exists():
    load_dotenv(env_file)


def make_variant_name(
    benchmark: str,
    suffix: str,
    sanitizer: str = "address",
) -> str:
    """Generate variant name with sanitizer (new naming convention).

    Since multi-sanitizer support, variant names include sanitizer to prevent
    naming conflicts when building same variant with different sanitizers.

    Naming convention:
    - Format: {benchmark}-{san_short}-{suffix}
    - Examples: "bench-asan-deltaref", "bench-ubsan-delta-cpv0"

    Args:
        benchmark: Benchmark name (e.g., "test-proj")
        suffix: Variant suffix (e.g., "deltaref", "delta-cpv0")
        sanitizer: Full sanitizer name (default: "address")

    Returns:
        Full variant name (e.g., "test-proj-asan-deltaref")
    """
    # Map sanitizer to short name (consistent with builder.types.sanitizer_short_name)
    san_short_map = {
        "address": "asan",
        "undefined": "ubsan",
        "memory": "msan",
        "thread": "tsan",
        "coverage": "coverage",
    }
    san_short = san_short_map.get(sanitizer, sanitizer)
    return f"{benchmark}-{san_short}-{suffix}"


def make_job_id(
    job_type: str,
    benchmark: str,
    variant_suffix: Optional[str] = None,
    sanitizer: str = "address",
    extra: Optional[str] = None,
) -> str:
    """Generate job ID with variant name including sanitizer.

    Args:
        job_type: Job type (e.g., "build-single", "verify-cpv-pov")
        benchmark: Benchmark name
        variant_suffix: Variant suffix if applicable
        sanitizer: Full sanitizer name (default: "address")
        extra: Extra components for job ID (e.g., ":cpv_0")

    Returns:
        Job ID (e.g., "build-single:bench:bench-asan-deltaref")
    """
    if variant_suffix:
        variant_name = make_variant_name(benchmark, variant_suffix, sanitizer)
        job_id = f"{job_type}:{benchmark}:{variant_name}"
    else:
        job_id = f"{job_type}:{benchmark}"

    if extra:
        job_id = f"{job_id}{extra}"

    return job_id
