# Benchmark CI Design Document

## Overview

This document describes the design of the Benchmark CI module for validating benchmark data integrity. The module uses a **hybrid job-executor pattern with two-phase execution** to efficiently build variants and verify POVs/patches.

### Goals

1. **Validate benchmark data**: Ensure POVs trigger correct CPVs, patches fix vulnerabilities
2. **Track all operations**: Build time, verify time, logs, artifacts for every job
3. **Efficient execution**: Deduplicate builds, parallel verification
4. **Support local and GitHub CI**: Same logic, different execution environments
5. **Reuse existing infrastructure**: Leverage `OSSFuzzBuilder`, `VerdictResolver`, etc.

### Non-Goals

- Running CRS experiments (handled by `crsbench/evaluation/`)
- Distributed execution across machines (use `crsbench evaluator --ci`, see `crsbench/distributed/`)

## Architecture

### Two-Phase Execution Model

```
┌─────────────────────────────────────────────────────────────┐
│                     Job Factory                              │
│  Creates BuildJob and VerifyJob instances with dependencies │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                   Phase 1: Build                             │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐            │
│  │ BuildJob    │ │ BuildJob    │ │ BuildJob    │  ...       │
│  │ deltabase   │ │ deltaref    │ │ cpv0        │            │
│  └─────────────┘ └─────────────┘ └─────────────┘            │
│                    (parallel via OSSFuzzBuilder)             │
└─────────────────────────────────────────────────────────────┘
                              │ all builds complete
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                   Phase 2: Verify                            │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐            │
│  │ VerifyJob   │ │ VerifyJob   │ │ VerifyJob   │  ...       │
│  │ pov0:delta  │ │ pov0:cpv0   │ │ pov1:delta  │            │
│  └─────────────┘ └─────────────┘ └─────────────┘            │
│                    (parallel via ThreadPoolExecutor)         │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                   Verdict Resolution                         │
│  Uses existing VerdictResolver to determine final status     │
└─────────────────────────────────────────────────────────────┘
```

### Why Two-Phase?

1. **No race conditions**: All builds complete before verification starts
2. **Natural deduplication**: Collect all required variants, build once
3. **Simple coordination**: No locks needed between jobs
4. **Clear dependency**: Verify jobs depend on build jobs explicitly

## Job Abstraction

### Base Job Class

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

@dataclass
class JobResult:
    """Result of any job execution."""
    job_id: str
    job_type: str  # "build", "verify-pov", "verify-patch"
    success: bool
    started_at: datetime
    finished_at: datetime
    elapsed_seconds: float
    logs: str = ""
    error: Optional[str] = None
    artifacts: dict[str, Path] = field(default_factory=dict)
    details: dict = field(default_factory=dict)


class Job(ABC):
    """Base job interface."""

    @property
    @abstractmethod
    def job_id(self) -> str:
        """Unique identifier: {job_type}:{benchmark}-{sanitizer}-{variant}"""
        ...

    @property
    @abstractmethod
    def job_type(self) -> str:
        """Type: build, verify-pov, verify-patch"""
        ...

    @property
    def depends_on(self) -> list[str]:
        """Job IDs this job depends on (for dependency graph)."""
        return []

    @abstractmethod
    def execute(self, context: JobContext) -> JobResult:
        """Execute the job."""
        ...
```

### Build Job

```python
@dataclass
class BuildJob(Job):
    """Build a variant - tracked with time, logs, artifacts."""
    benchmark: str
    sanitizer: str
    variant_type: str  # "deltabase", "deltaref", "cpv0", etc.
    config: BuildConfig

    @property
    def job_id(self) -> str:
        return f"build:{self.benchmark}-{self.sanitizer}-{self.variant_type}"

    @property
    def job_type(self) -> str:
        return "build"

    @property
    def variant_name(self) -> str:
        return f"{self.benchmark}-{self.sanitizer}-{self.variant_type}"

    def execute(self, context: JobContext) -> JobResult:
        started_at = datetime.now()

        # Reuse OSSFuzzBuilder
        result = context.builder.build_single(self.config)

        finished_at = datetime.now()
        return JobResult(
            job_id=self.job_id,
            job_type=self.job_type,
            success=result.success,
            started_at=started_at,
            finished_at=finished_at,
            elapsed_seconds=result.elapsed_seconds,
            error=result.error,
            artifacts={"build_path": result.build_path} if result.build_path else {},
        )
```

### Verify POV Job

```python
@dataclass
class VerifyPovJob(Job):
    """Verify a POV against a variant."""
    benchmark: str
    sanitizer: str
    variant_type: str
    pov_id: str
    pov_path: Path
    harness: str
    expected_crash: bool

    @property
    def job_id(self) -> str:
        return f"verify-pov:{self.benchmark}-{self.sanitizer}-{self.variant_type}:{self.pov_id}"

    @property
    def job_type(self) -> str:
        return "verify-pov"

    @property
    def depends_on(self) -> list[str]:
        return [f"build:{self.benchmark}-{self.sanitizer}-{self.variant_type}"]

    def execute(self, context: JobContext) -> JobResult:
        started_at = datetime.now()
        variant_name = f"{self.benchmark}-{self.sanitizer}-{self.variant_type}"

        # Reuse OSSFuzzInfrastructure.reproduce()
        output = context.infra.reproduce(
            project_name=variant_name,
            harness=self.harness,
            pov_data=self.pov_path.read_bytes(),
            timeout=context.timeout,
            pov_id=self.pov_id,
        )

        finished_at = datetime.now()
        actual_crash = output.crashed
        success = actual_crash == self.expected_crash

        return JobResult(
            job_id=self.job_id,
            job_type=self.job_type,
            success=success,
            started_at=started_at,
            finished_at=finished_at,
            elapsed_seconds=(finished_at - started_at).total_seconds(),
            logs=output.stdout if output.crashed else "",
            error=self._get_error_message(actual_crash) if not success else None,
            details={
                "expected_crash": self.expected_crash,
                "actual_crash": actual_crash,
                "variant": variant_name,
            },
        )

    def _get_error_message(self, actual_crash: bool) -> str:
        if self.expected_crash and not actual_crash:
            return f"POV {self.pov_id} did NOT crash (expected crash)"
        if not self.expected_crash and actual_crash:
            if "deltabase" in self.variant_type:
                return f"ZERODAY: POV {self.pov_id} crashed on deltabase"
            return f"POV {self.pov_id} crashed (expected no crash)"
        return ""
```

### Verify Patch Job

```python
@dataclass
class VerifyPatchJob(Job):
    """Verify a patch fixes all POVs for its CPV."""
    benchmark: str
    sanitizer: str
    cpv_num: int
    patch_path: Path
    povs_for_cpv: list[tuple[str, Path]]  # [(pov_id, pov_path), ...]
    harness: str

    @property
    def job_id(self) -> str:
        return f"verify-patch:{self.benchmark}-{self.sanitizer}-cpv{self.cpv_num}"

    @property
    def job_type(self) -> str:
        return "verify-patch"

    @property
    def depends_on(self) -> list[str]:
        return [f"build:{self.benchmark}-{self.sanitizer}-patched-cpv{self.cpv_num}"]

    def execute(self, context: JobContext) -> JobResult:
        started_at = datetime.now()
        variant_name = f"{self.benchmark}-{self.sanitizer}-patched-cpv{self.cpv_num}"

        failed_povs = []
        for pov_id, pov_path in self.povs_for_cpv:
            output = context.infra.reproduce(
                project_name=variant_name,
                harness=self.harness,
                pov_data=pov_path.read_bytes(),
                timeout=context.timeout,
                pov_id=pov_id,
            )
            if output.crashed:
                failed_povs.append(pov_id)

        finished_at = datetime.now()
        success = len(failed_povs) == 0

        return JobResult(
            job_id=self.job_id,
            job_type=self.job_type,
            success=success,
            started_at=started_at,
            finished_at=finished_at,
            elapsed_seconds=(finished_at - started_at).total_seconds(),
            error=f"Patch does not fix: {failed_povs}" if failed_povs else None,
            details={
                "total_povs": len(self.povs_for_cpv),
                "fixed": len(self.povs_for_cpv) - len(failed_povs),
                "failed": failed_povs,
            },
        )
```

## Variant Naming Convention

Format: `{benchmark}-{sanitizer}-{variant_type}`

### POV Verification Variants

| Variant Type | Example | Description |
|--------------|---------|-------------|
| `deltabase` | `curl-asan-deltabase` | Pre-vulnerability state (should NOT crash) |
| `deltaref` | `curl-asan-deltaref` | Vulnerable state (should crash) |
| `fullbase` | `curl-asan-fullbase` | Vulnerable state for FULL mode |
| `allpatched` | `curl-asan-allpatched` | All patches applied (should NOT crash) |
| `cpv{N}` | `curl-asan-cpv0` | All patches except patch_N (should crash for pov_N) |

### Patch Verification Variants

| Variant Type | Example | Description |
|--------------|---------|-------------|
| `patched-cpv{N}` | `curl-asan-patched-cpv0` | Patch for CPV_N applied (built with inc-build) |

## Verification Logic

### POV Verification Matrix (DELTA Mode)

For N POVs/CPVs, build 3 + N variants:

| Variant | pov_0 | pov_1 | ... | pov_N |
|---------|-------|-------|-----|-------|
| deltabase | ❌ no crash | ❌ no crash | ... | ❌ no crash |
| deltaref | ✅ crash | ✅ crash | ... | ✅ crash |
| allpatched | ❌ no crash | ❌ no crash | ... | ❌ no crash |
| cpv0 | ✅ crash | ❌ no crash | ... | ❌ no crash |
| cpv1 | ❌ no crash | ✅ crash | ... | ❌ no crash |
| ... | ... | ... | ... | ... |
| cpvN | ❌ no crash | ❌ no crash | ... | ✅ crash |

### Verdict Resolution

Reuse existing `VerdictResolver` from `crsbench/evaluation/verification/pov/verdict.py`:

```python
from crsbench.evaluation.verification.pov.verdict import VerdictResolver
from crsbench.builder.types import BenchmarkMode, VariantType

def resolve_pov_verdict(
    mode: BenchmarkMode,
    pov_id: str,
    crash_results: dict[str, bool],  # {variant_type: crashed}
    benchmark: str,
) -> PovVerificationResult:
    """Resolve verdict for a single POV using existing VerdictResolver."""

    # Convert to VariantType keys
    variant_crash_map = {}
    cpv_crash_map = {}

    for variant_type, crashed in crash_results.items():
        if variant_type.startswith("cpv"):
            cpv_num = int(variant_type.replace("cpv", ""))
            cpv_crash_map[cpv_num] = crashed
        else:
            vt = VariantType(variant_type)
            variant_crash_map[vt] = crashed

    return VerdictResolver.resolve(
        mode=mode,
        crash_results=variant_crash_map,
        cpv_crash_map=cpv_crash_map,
        benchmark_name=benchmark,
        pov_id=pov_id,
    )
```

### Verdict Types

From existing `PovVerificationStatus`:

| Status | Description |
|--------|-------------|
| `CPV` | POV correctly matches one or more CPVs |
| `ZERODAY` | POV crashes on deltabase (pre-vulnerability) |
| `NOT_VULNERABLE` | POV doesn't crash on vulnerable version |
| `UNINTENDED_CRASH` | Crashes on allpatched or unexpected pattern |

## Job Factory

```python
class JobFactory:
    """Creates jobs for benchmark CI."""

    def __init__(self, adapter: MetaYamlAdapter, sanitizer: str = "address"):
        self.adapter = adapter
        self.sanitizer = sanitizer
        self.benchmark = adapter.benchmark_name
        self.mode = adapter.get_mode()

    def create_all_jobs(self) -> list[Job]:
        """Create all jobs for POV and patch verification."""
        jobs = []
        jobs.extend(self._create_build_jobs())
        jobs.extend(self._create_verify_pov_jobs())
        jobs.extend(self._create_verify_patch_jobs())
        return jobs

    def _create_build_jobs(self) -> list[BuildJob]:
        """Create build jobs for all required variants."""
        jobs = []
        povs = list(self.adapter.get_all_povs())
        num_cpvs = len(set(pov.cpv_num for _, pov in povs))

        # Base variants
        if self.mode == BenchmarkMode.DELTA:
            jobs.append(self._make_build_job("deltabase", VariantType.DELTA_BASE))
            jobs.append(self._make_build_job("deltaref", VariantType.DELTA_REF))
        else:
            jobs.append(self._make_build_job("fullbase", VariantType.FULL_BASE))

        # Allpatched
        jobs.append(self._make_build_job("allpatched", VariantType.ALL_PATCHED))

        # CPV variants
        for i in range(num_cpvs):
            jobs.append(self._make_build_job(f"cpv{i}", VariantType.CPV, cpv_num=i))

        # Patched variants (for patch verification)
        for i in range(num_cpvs):
            jobs.append(self._make_build_job(
                f"patched-cpv{i}",
                VariantType.PATCHED,
                cpv_num=i,
                use_inc_build=True,
            ))

        return jobs

    def _create_verify_pov_jobs(self) -> list[VerifyPovJob]:
        """Create verify jobs for all POV × variant combinations."""
        jobs = []
        povs = list(self.adapter.get_all_povs())
        num_cpvs = len(set(pov.cpv_num for _, pov in povs))

        # Determine which variants to test against
        if self.mode == BenchmarkMode.DELTA:
            base_variants = [
                ("deltabase", False),  # Should NOT crash
                ("deltaref", True),    # Should crash
            ]
        else:
            base_variants = [
                ("fullbase", True),    # Should crash
            ]

        base_variants.append(("allpatched", False))  # Should NOT crash

        for harness, vuln_keyword, pov in povs:
            pov_path = self.adapter.get_pov_path(harness, vuln_keyword, pov.id)
            cpv_num = pov.cpv_num

            # Test against base variants
            for variant_type, expected_crash in base_variants:
                jobs.append(VerifyPovJob(
                    benchmark=self.benchmark,
                    sanitizer=self.sanitizer,
                    variant_type=variant_type,
                    pov_id=pov.id,
                    pov_path=pov_path,
                    harness=harness,
                    expected_crash=expected_crash,
                ))

            # Test against CPV variants
            for i in range(num_cpvs):
                expected_crash = (i == cpv_num)  # Only crashes on its own CPV
                jobs.append(VerifyPovJob(
                    benchmark=self.benchmark,
                    sanitizer=self.sanitizer,
                    variant_type=f"cpv{i}",
                    pov_id=pov.id,
                    pov_path=pov_path,
                    harness=harness,
                    expected_crash=expected_crash,
                ))

        return jobs

    def _create_verify_patch_jobs(self) -> list[VerifyPatchJob]:
        """Create patch verification jobs."""
        jobs = []

        # Group POVs by CPV
        cpv_povs: dict[int, list[tuple[str, Path]]] = defaultdict(list)
        for harness, vuln_keyword, pov in self.adapter.get_all_povs():
            pov_path = self.adapter.get_pov_path(harness, vuln_keyword, pov.id)
            cpv_povs[pov.cpv_num].append((pov.id, pov_path))

        for cpv_num, povs in cpv_povs.items():
            patch_path = self.adapter.get_patch_path_for_cpv(cpv_num)
            harness = self.adapter.get_harness_for_cpv(cpv_num)

            jobs.append(VerifyPatchJob(
                benchmark=self.benchmark,
                sanitizer=self.sanitizer,
                cpv_num=cpv_num,
                patch_path=patch_path,
                povs_for_cpv=povs,
                harness=harness,
            ))

        return jobs
```

## Runner

```python
class ProjectCIRunner:
    """Runs CI for a single project using two-phase execution."""

    def __init__(
        self,
        oss_fuzz_path: Path,
        build_workers: int = 4,
        verify_workers: int = 4,
    ):
        self.builder = OSSFuzzBuilder(oss_fuzz_path, max_workers=build_workers)
        self.infra = self.builder.infra
        self.verify_workers = verify_workers

    def run(self, jobs: list[Job]) -> ProjectCIResult:
        """Execute jobs in two phases."""
        started_at = datetime.now()

        # Separate jobs by type
        build_jobs = [j for j in jobs if j.job_type == "build"]
        verify_jobs = [j for j in jobs if j.job_type.startswith("verify")]

        results: dict[str, JobResult] = {}

        # Phase 1: Build all variants
        logger.info(f"=== Build Phase: {len(build_jobs)} jobs ===")
        build_results = self._execute_build_phase(build_jobs)
        results.update(build_results)

        # Check for build failures
        failed_builds = {jid for jid, r in build_results.items() if not r.success}

        # Phase 2: Verify (skip if dependencies failed)
        logger.info(f"=== Verify Phase: {len(verify_jobs)} jobs ===")
        verify_results = self._execute_verify_phase(verify_jobs, failed_builds)
        results.update(verify_results)

        finished_at = datetime.now()

        return ProjectCIResult(
            started_at=started_at,
            finished_at=finished_at,
            results=list(results.values()),
        )

    def _execute_build_phase(self, jobs: list[BuildJob]) -> dict[str, JobResult]:
        """Execute build jobs using OSSFuzzBuilder."""
        results = {}

        # Extract BuildConfigs and use builder's parallel execution
        configs = [job.config for job in jobs]
        build_results = self.builder.build_variants(configs)

        # Convert to JobResults
        for job in jobs:
            br = build_results.get(job.config.variant_name)
            if br:
                results[job.job_id] = JobResult(
                    job_id=job.job_id,
                    job_type="build",
                    success=br.success,
                    started_at=datetime.now(),  # Approximation
                    finished_at=datetime.now(),
                    elapsed_seconds=br.elapsed_seconds,
                    error=br.error,
                    artifacts={"build_path": br.build_path} if br.build_path else {},
                )
                status = "PASS" if br.success else "FAIL"
                logger.info(f"[{job.job_id}] {status} ({br.elapsed_seconds:.1f}s)")

        return results

    def _execute_verify_phase(
        self,
        jobs: list[Job],
        failed_builds: set[str],
    ) -> dict[str, JobResult]:
        """Execute verify jobs in parallel."""
        results = {}
        context = JobContext(
            builder=self.builder,
            infra=self.infra,
            timeout=120,
        )

        with ThreadPoolExecutor(max_workers=self.verify_workers) as executor:
            futures = {}

            for job in jobs:
                # Skip if dependency failed
                deps_failed = any(dep in failed_builds for dep in job.depends_on)
                if deps_failed:
                    results[job.job_id] = JobResult(
                        job_id=job.job_id,
                        job_type=job.job_type,
                        success=False,
                        started_at=datetime.now(),
                        finished_at=datetime.now(),
                        elapsed_seconds=0,
                        error="Dependency build failed",
                    )
                    logger.warning(f"[{job.job_id}] SKIP (dependency failed)")
                else:
                    futures[executor.submit(job.execute, context)] = job

            for future in as_completed(futures):
                job = futures[future]
                try:
                    result = future.result()
                    results[job.job_id] = result
                    status = "PASS" if result.success else "FAIL"
                    logger.info(f"[{job.job_id}] {status} ({result.elapsed_seconds:.1f}s)")
                except Exception as e:
                    results[job.job_id] = JobResult(
                        job_id=job.job_id,
                        job_type=job.job_type,
                        success=False,
                        started_at=datetime.now(),
                        finished_at=datetime.now(),
                        elapsed_seconds=0,
                        error=str(e),
                    )
                    logger.error(f"[{job.job_id}] ERROR: {e}")

        return results
```

## Result Collection

Adapt from `feat/benchmark-ci-2`:

```python
@dataclass
class ProjectCIResult:
    """Complete CI result for a project."""
    started_at: datetime
    finished_at: datetime
    results: list[JobResult]

    @property
    def build_results(self) -> list[JobResult]:
        return [r for r in self.results if r.job_type == "build"]

    @property
    def verify_results(self) -> list[JobResult]:
        return [r for r in self.results if r.job_type.startswith("verify")]

    @property
    def total_build_time(self) -> float:
        return sum(r.elapsed_seconds for r in self.build_results)

    @property
    def passed(self) -> bool:
        return all(r.success for r in self.results)

    def get_summary(self) -> dict:
        return {
            "total_jobs": len(self.results),
            "passed": sum(1 for r in self.results if r.success),
            "failed": sum(1 for r in self.results if not r.success),
            "build_time_seconds": self.total_build_time,
            "verify_time_seconds": sum(r.elapsed_seconds for r in self.verify_results),
            "duration_seconds": (self.finished_at - self.started_at).total_seconds(),
        }

    def to_csv(self, path: Path) -> None:
        """Export results to CSV."""
        # Reuse ResultCollector from benchmark-ci-2
        ...
```

## CLI Interface

```python
# crsbench/benchmark_ci/cli.py

@click.command()
@click.argument("benchmark_path", type=click.Path(exists=True))
@click.option("--sanitizer", default="address", help="Sanitizer to use")
@click.option("--build-workers", default=4, help="Parallel build workers")
@click.option("--verify-workers", default=4, help="Parallel verify workers")
@click.option("--output", type=click.Path(), help="Output CSV path")
@click.option("--dry-run", is_flag=True, help="Show jobs without executing")
def ci(benchmark_path, sanitizer, build_workers, verify_workers, output, dry_run):
    """Run CI validation for a benchmark."""
    adapter = MetaYamlAdapter.from_benchmark_path(Path(benchmark_path))

    # Create jobs
    factory = JobFactory(adapter, sanitizer=sanitizer)
    jobs = factory.create_all_jobs()

    if dry_run:
        _print_job_plan(jobs)
        return

    # Run
    runner = ProjectCIRunner(
        oss_fuzz_path=get_oss_fuzz_root(),
        build_workers=build_workers,
        verify_workers=verify_workers,
    )
    result = runner.run(jobs)

    # Report
    _print_summary(result)

    if output:
        result.to_csv(Path(output))

    sys.exit(0 if result.passed else 1)
```

## GitHub CI Integration

```yaml
# .github/workflows/benchmark-ci.yml
name: Benchmark CI

on:
  push:
    paths:
      - 'benchmarks/**'
  pull_request:
    paths:
      - 'benchmarks/**'

jobs:
  ci:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        benchmark:
          - curl
          - openssl
          - sqlite3

    steps:
      - uses: actions/checkout@v4
        with:
          submodules: recursive

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: |
          pip install uv
          uv sync

      - name: Run benchmark CI
        run: |
          uv run crsbench benchmark ci benchmarks/${{ matrix.benchmark }} \
            --sanitizer address \
            --output results-${{ matrix.benchmark }}.csv

      - name: Upload results
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: ci-results-${{ matrix.benchmark }}
          path: results-${{ matrix.benchmark }}.csv
```

## Component Reuse Summary

| Component | Source | Usage |
|-----------|--------|-------|
| `OSSFuzzBuilder` | `crsbench/builder/` | Build variants |
| `OSSFuzzInfrastructure` | `crsbench/builder/` | reproduce() |
| `VerdictResolver` | `crsbench/evaluation/verification/pov/` | Verdict logic |
| `MetaYamlAdapter` | `crsbench/validation/` | Benchmark config |
| `BuildConfig`, `VariantType` | `crsbench/builder/types.py` | Type definitions |
| `ResultCollector` | `feat/benchmark-ci-2` | CSV export |

## File Structure

```
crsbench/benchmark_ci/
├── __init__.py
├── cli.py              # CLI entry point
├── runner.py           # ProjectCIRunner
├── jobs/
│   ├── __init__.py
│   ├── base.py         # Job, JobResult, JobContext
│   ├── build.py        # BuildJob
│   └── verify.py       # VerifyPovJob, VerifyPatchJob
├── factory.py          # JobFactory
├── models.py           # ProjectCIResult, summaries
└── verdict.py          # Verdict resolution (wraps VerdictResolver)
```

## Example Output

```
$ crsbench benchmark ci benchmarks/curl --sanitizer address

=== Build Phase: 5 jobs ===
[build:curl-asan-deltabase]    PASS (245.3s)
[build:curl-asan-deltaref]     PASS (251.1s)
[build:curl-asan-allpatched]   PASS (248.7s)
[build:curl-asan-cpv0]         PASS (250.2s)
[build:curl-asan-cpv1]         PASS (249.8s)

=== Verify Phase: 12 jobs ===
[verify-pov:curl-asan-deltabase:pov_0]    PASS - no crash (2.1s)
[verify-pov:curl-asan-deltaref:pov_0]     PASS - crashed (2.3s)
[verify-pov:curl-asan-allpatched:pov_0]   PASS - no crash (2.0s)
[verify-pov:curl-asan-cpv0:pov_0]         PASS - crashed (2.2s)
[verify-pov:curl-asan-cpv1:pov_0]         PASS - no crash (1.9s)
...

=== Verdict Summary ===
POV pov_0: CPV (matched cpv_0) ✓
POV pov_1: CPV (matched cpv_1) ✓

=== Patch Verification ===
[verify-patch:curl-asan-cpv0]  PASS - fixes 1/1 POVs (52.3s)
[verify-patch:curl-asan-cpv1]  PASS - fixes 1/1 POVs (51.8s)

=== Summary ===
Benchmark: curl
Total: 17 jobs (17 passed, 0 failed)
Build time: 1245.1s
Verify time: 130.5s
Status: PASSED ✓
```
