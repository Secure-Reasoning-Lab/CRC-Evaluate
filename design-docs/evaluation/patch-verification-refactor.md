# Patch Verification Architecture Refactor

## Overview

This document describes the architectural refactor of the patch verification module to:
1. Fix race condition in parallel builds
2. Align with POV verification architecture (OSSFuzzBuilder)
3. Enable future optimizations (hash-based dedup, build caching)

## Problem Statement

### Race Condition in Current Implementation

Current `build_with_inc_image()` uses shared output paths:

```python
# infrastructure.py lines 821-824
out_dir = self.oss_fuzz_path / "build" / "out" / project_name
work_dir = self.oss_fuzz_path / "build" / "work" / project_name
```

When multiple `verify_patch()` calls run in parallel, they all write to the same directories, causing:
- Build artifact corruption
- Non-deterministic verification results
- False positives/negatives

### Architecture Inconsistency

| Aspect | POV Verification | Patch Verification |
|--------|------------------|-------------------|
| Builder | `OSSFuzzBuilder` | `OSSFuzzInfrastructure` (direct) |
| Parallel builds | Yes (via `execute_plan()`) | No (race condition) |
| Build/verify workers | Separate | Single `verify_workers` |
| Build isolation | Per-variant paths | Shared paths (bug) |

## Solution Design

### 1. Variant Type Extension

Add `PATCHED` variant type to `crsbench/builder/types.py`:

```python
class VariantType(Enum):
    # Existing types
    FULL_BASE = "fullbase"
    DELTA_BASE = "deltabase"
    DELTA_REF = "deltaref"
    ALL_PATCHED = "allpatched"
    CPV = "cpv"
    COVERAGE = "coverage"

    # New type for patch verification
    PATCHED = "patched"
```

### 2. BuildConfig Extension

Extend `BuildConfig` in `crsbench/builder/types.py`:

```python
@dataclass
class BuildConfig:
    # Existing fields...

    # New fields for PATCHED variant
    patch_id: Optional[str] = None       # Patch identifier
    pov_id: Optional[str] = None         # Source POV/CPV identifier
    patch_content: Optional[str] = None  # For hash-based dedup (future)
    use_inc_build: bool = False          # Use inc-build image

    @property
    def variant_name(self) -> str:
        """Generate unique variant name for build isolation."""
        if self.variant_type == VariantType.PATCHED:
            # Format: {benchmark}-{mode}-patched-{pov_id}-{patch_id}
            # Includes mode for consistency with other variants
            mode_str = "delta" if self.mode == BenchmarkMode.DELTA else "full"
            return f"{self.benchmark_name}-{mode_str}-patched-{self.pov_id}-{self.patch_id}"
        # ... existing logic for other types
```

**Note**: Boolean `use_inc_build` should be keyword-only in method signatures per coding standards.

### 3. OSSFuzzBuilder Extension

Add patch build plan creation to `crsbench/builder/builder.py`:

```python
class OSSFuzzBuilder:
    def create_patch_build_plan(
        self,
        benchmark_name: str,
        benchmark_path: Path,
        main_repo: str,
        commit: str,
        patches: list[PatchInfo],
        language: str = "c",
        repo_name: Optional[str] = None,
        sanitizer: str = "address",
        *,
        use_inc_build: bool = True,
    ) -> BuildPlan:
        """Create build plan for patch verification.

        Each patch gets isolated build via unique variant_name.

        Args:
            benchmark_name: Benchmark name
            benchmark_path: Path to benchmark
            main_repo: Repository URL
            commit: Commit to apply patches to
            patches: List of PatchInfo to verify
            language: Programming language
            repo_name: Optional repo name for caching
            sanitizer: Sanitizer type
            use_inc_build: Use inc-build images if available

        Returns:
            BuildPlan with one config per patch
        """
        plan = BuildPlan(benchmark_name=benchmark_name)

        for patch in patches:
            config = BuildConfig(
                benchmark_name=benchmark_name,
                variant_type=VariantType.PATCHED,
                commit=commit,
                main_repo=main_repo,
                benchmark_path=benchmark_path,
                mode=BenchmarkMode.DELTA,  # Patches apply to ref commit
                patches=[patch.patch_path],
                patch_id=patch.patch_id,
                pov_id=patch.pov_id,
                language=language,
                repo_name=repo_name,
                use_inc_build=use_inc_build,
            )
            plan.add_config(config)

        return plan
```

### 4. Build Method for PATCHED Variants

Extend `_build_single()` to handle PATCHED variants:

```python
def _build_single(self, config: BuildConfig) -> BuildResult:
    # ... existing setup ...

    if config.variant_type == VariantType.PATCHED:
        if config.use_inc_build and self._has_inc_build_image(config):
            return self._build_with_inc_image(config, repo_path)
        else:
            # Fallback to standard build
            return self._build_standard(config, repo_path)

    # ... existing logic for other types
```

### 5. Result Model Alignment

Update `PatchVerificationResult` in `crsbench/evaluation/verification/models.py`:

```python
@dataclass
class PatchVerificationResult:
    """Result of patch verification."""

    # Identity
    patch_id: str              # Patch identifier (e.g., "patch_0")
    pov_id: str                # Source POV/CPV (e.g., "pov_0", "cpv_0")

    # Context (aligned with PovVerificationResult)
    benchmark: str             # NEW: Benchmark name
    harness: str               # Harness tested
    patch_path: Path

    # Verification results
    status: PatchVerificationStatus
    cpv_fixed: list[str]       # CPVs fully fixed (all variants pass)
    cpv_stats: dict[str, CpvStats]
    scores: Optional[VerificationScores]
    security_verdict: SecurityVerdict = "FAIL"

    # Build/test details
    build_time: Optional[float] = None
    pov_test_passed: bool = False
    unit_tests_passed: Optional[bool] = None
    unit_tests_run: int = 0
    unit_tests_failed: int = 0
    failed_tests: list[str] = field(default_factory=list)
    details: Optional[str] = None

    @property
    def variant_name(self) -> str:
        """Get the build variant name."""
        return f"{self.benchmark}-patched-{self.pov_id}-{self.patch_id}"
```

### 6. PatchInfo Model Update

Update `PatchInfo` to include both identifiers:

```python
@dataclass
class PatchInfo:
    """Information about a patch to verify."""
    patch_id: str          # Patch identifier
    pov_id: str            # Source POV/CPV identifier
    patch_path: Path       # Path to patch file
    patch_content: Optional[str] = None  # Loaded content (for hashing)

    @classmethod
    def from_crs_output(cls, patch_dir: Path) -> "PatchInfo":
        """Create from CRS output structure: patches/<pov_id>/patch.diff"""
        pov_id = patch_dir.name
        patch_path = patch_dir / "patch.diff"
        return cls(
            patch_id="patch_0",  # CRS typically generates one patch per POV
            pov_id=pov_id,
            patch_path=patch_path,
        )

    @classmethod
    def from_benchmark(cls, patch_path: Path, cpv_id: str) -> "PatchInfo":
        """Create from benchmark structure: .aixcc/<harness>/<cpv>/patches/patch_*.diff"""
        patch_id = patch_path.stem  # e.g., "patch_0"
        return cls(
            patch_id=patch_id,
            pov_id=cpv_id,  # Use CPV as pov_id for ground-truth
            patch_path=patch_path,
        )
```

### 7. PatchVerificationEngine Refactor

Refactor `crsbench/evaluation/verification/patch/engine.py`:

```python
class PatchVerificationEngine:
    """Engine for verifying CRS-generated patches."""

    def __init__(
        self,
        oss_fuzz_path: Path,
        build_workers: int = 4,
        verify_workers: int = 4,
        sanitizer: str = "address",
        timeout: int = 120,
        build_timeout: int = 1200,
        test_timeout: int = 1800,
        test_mode: TestMode = TestMode.FULL,
        *,
        verify_variants: bool = True,
    ):
        self.oss_fuzz_path = Path(oss_fuzz_path)
        self.builder = OSSFuzzBuilder(oss_fuzz_path, max_workers=build_workers)
        self.verify_workers = verify_workers
        self.sanitizer = sanitizer
        self.timeout = timeout
        self.build_timeout = build_timeout
        self.test_timeout = test_timeout
        self.test_mode = test_mode
        self.verify_variants = verify_variants

    def verify_patches(
        self,
        benchmark_path: Path,
        patches: list[PatchInfo],
        harness: str,
        *,
        parallel: bool = True,
    ) -> list[PatchVerificationResult]:
        """Verify multiple patches with parallel builds.

        Phase 1: Build all patches in parallel (isolated paths)
        Phase 2: Verify each build against POVs (parallel)
        """
        adapter = MetaYamlAdapter(benchmark_path)

        # Phase 1: Parallel builds via OSSFuzzBuilder
        plan = self.builder.create_patch_build_plan(
            benchmark_name=adapter.benchmark_name,
            benchmark_path=benchmark_path,
            main_repo=adapter.main_repo,
            commit=adapter.get_ref_commit() or adapter.get_base_commit(),
            patches=patches,
            language=adapter.lang,
            repo_name=adapter.repo_name,
            sanitizer=self.sanitizer,
            use_inc_build=True,
        )

        build_results = self.builder.execute_plan(plan)

        # Phase 2: Parallel verification
        results: list[PatchVerificationResult] = []

        if not parallel:
            for patch in patches:
                result = self._verify_built_patch(
                    patch, build_results, adapter, harness
                )
                results.append(result)
        else:
            with ThreadPoolExecutor(max_workers=self.verify_workers) as executor:
                futures = {
                    executor.submit(
                        self._verify_built_patch,
                        patch,
                        build_results,
                        adapter,
                        harness,
                    ): patch
                    for patch in patches
                }

                for future in as_completed(futures):
                    results.append(future.result())

        return results

    def _verify_built_patch(
        self,
        patch: PatchInfo,
        build_results: dict[str, BuildResult],
        adapter: MetaYamlAdapter,
        harness: str,
    ) -> PatchVerificationResult:
        """Verify a single built patch against POVs."""
        variant_name = f"{adapter.benchmark_name}-patched-{patch.pov_id}-{patch.patch_id}"
        build_result = build_results.get(variant_name)

        result = PatchVerificationResult(
            patch_id=patch.patch_id,
            pov_id=patch.pov_id,
            benchmark=adapter.benchmark_name,
            harness=harness,
            patch_path=patch.patch_path,
            status=PatchVerificationStatus.PENDING,
        )

        # Check build result
        if not build_result or not build_result.success:
            result.status = PatchVerificationStatus.BUILD_FAILED
            result.details = build_result.error if build_result else "Build not found"
            result.build_time = build_result.elapsed_seconds if build_result else None
            return result

        result.build_time = build_result.elapsed_seconds

        # Test against all CPVs in harness
        cpv_fixed, cpv_stats, scores = self._test_patch_against_all_cpvs(
            variant_name, harness, adapter.benchmark_path
        )

        result.cpv_fixed = cpv_fixed
        result.cpv_stats = cpv_stats
        result.scores = scores
        result.pov_test_passed = len(cpv_fixed) > 0

        if not result.pov_test_passed:
            result.status = PatchVerificationStatus.POV_STILL_TRIGGERS
            result.details = f"No CPVs fixed. Partial: {scores.cpvs_partial}"
            result.security_verdict = "FAIL"
            return result

        # Run unit tests
        test_passed, test_details = self._run_unit_tests(
            variant_name, build_result.build_path
        )
        result.unit_tests_passed = test_passed

        if not test_passed:
            result.status = PatchVerificationStatus.TEST_FAILED
            result.details = test_details
            result.security_verdict = "FAIL"
            return result

        # All checks passed
        result.status = PatchVerificationStatus.VALID
        result.security_verdict = "PASS"
        return result
```

## Build Path Isolation

### Variant Naming Convention

```
{benchmark}-{mode}-patched-{pov_id}-{patch_id}
```

This includes `mode` (delta/full) for consistency with other variant types.

### Directory Structure

```
oss-fuzz/build/
├── out/
│   ├── benchmark-delta-patched-pov_0-patch_0/   # Patch 1 build
│   ├── benchmark-delta-patched-pov_0-patch_1/   # Patch 2 build
│   ├── benchmark-delta-patched-pov_1-patch_0/   # Patch 3 build
│   └── ...
└── work/
    ├── benchmark-delta-patched-pov_0-patch_0/   # Patch 1 work
    ├── benchmark-delta-patched-pov_0-patch_1/   # Patch 2 work
    └── ...
```

### Infrastructure Changes

`build_with_inc_image()` in `infrastructure.py` needs a new `variant_name` parameter:

```python
def build_with_inc_image(
    self,
    project_name: str,
    src_path: Path,
    repo_name: str,
    sanitizer: str = "address",
    timeout: int = 1200,
    variant_name: Optional[str] = None,  # NEW: for isolated build paths
    registry: str = "ghcr.io/team-atlanta/crsbench",
) -> bool:
    # Use variant_name for isolated paths, fallback to project_name
    path_id = variant_name or project_name
    out_dir = self.oss_fuzz_path / "build" / "out" / path_id
    work_dir = self.oss_fuzz_path / "build" / "work" / path_id
    ...
```

## CLI Changes

### New Arguments

```bash
crsbench patch-verify <benchmark> \
    --build-workers 4 \      # NEW: Parallel build workers
    --verify-workers 8 \     # Parallel verification workers
    --harness <harness> \
    --patch-dir <dir> \
    --pov-dir <dir>
```

### Environment Variables

```bash
CRSBENCH_BUILD_WORKERS=4    # Build parallelism
CRSBENCH_VERIFY_WORKERS=8   # Verification parallelism
```

## Edge Cases

### Empty Patches List

If `create_patch_build_plan()` is called with empty `patches` list:
- Return empty `BuildPlan`
- `verify_patches()` returns empty results list
- Log warning: "No patches to verify"

### Inc-Build Image Unavailability

If inc-build image doesn't exist for a project:
- Fallback to standard `build_fuzzers()` (full rebuild)
- Log warning about slower build
- Set `use_inc_build=False` in result metadata

```python
if config.use_inc_build and not self._has_inc_build_image(config):
    logger.warning(f"Inc-build image not available for {config.benchmark_name}, using full build")
    config.use_inc_build = False
```

### Patch Application Failure

If `git apply` fails:
- Return `BuildResult` with `success=False`
- Set `error="Patch application failed: <details>"`
- Do not proceed to compile step

### Duplicate Patches (Same Content)

Phase 1: Process all patches (may build duplicates)
Phase 2 (future): Hash-based dedup skips duplicate builds

### Concurrent POV Access

POV blob files are read-only during verification. Multiple patches can safely test against the same POVs concurrently.

### Missing Harness

If harness doesn't exist in built variant:
- Return `PatchVerificationResult` with `status=ERROR`
- Set `details="Harness not found: <harness>"`

## Future Enhancements

### Phase 2: Hash-Based Deduplication

```python
def get_patch_hash(patch_content: str) -> str:
    """Generate content hash for dedup."""
    return hashlib.sha256(patch_content.encode()).hexdigest()[:12]

# In create_patch_build_plan():
patch_hash = get_patch_hash(patch.patch_content)
if self._is_build_cached(patch_hash):
    plan.mark_cached(config.variant_name)
```

### Phase 3: Build Caching

```python
# Cache structure
.crsbench-cache/
└── builds/
    └── {patch_hash}/
        ├── out/        # Cached build artifacts
        └── metadata.json
```

## Migration Path

1. **Phase 1**: Add `VariantType.PATCHED` and `create_patch_build_plan()`
2. **Phase 2**: Refactor `PatchVerificationEngine` to use `OSSFuzzBuilder`
3. **Phase 3**: Update CLI with `--build-workers`
4. **Phase 4**: Update result models with `benchmark` field
5. **Phase 5**: Add tests for new architecture

## Files to Modify

| File | Changes |
|------|---------|
| `crsbench/builder/types.py` | Add `VariantType.PATCHED`, extend `BuildConfig` with `patch_id`, `pov_id`, `use_inc_build` |
| `crsbench/builder/builder.py` | Add `create_patch_build_plan()`, handle PATCHED in `_build_single()` |
| `crsbench/builder/infrastructure.py` | Add `variant_name` param to `build_with_inc_image()` for path isolation |
| `crsbench/evaluation/verification/models.py` | Update `PatchInfo` (add `patch_id`), `PatchVerificationResult` (add `benchmark`, rename) |
| `crsbench/evaluation/verification/patch/engine.py` | Refactor to use `OSSFuzzBuilder`, add `build_workers` param |
| `crsbench/evaluation/verification/cli/patch_verify_command.py` | Add `--build-workers` argument |
| `crsbench/utils/workers.py` | Ensure `resolve_build_workers()` exists (already present) |
| `tests/test_patch_verification.py` | Update tests for new architecture |

## Backward Compatibility

- Existing `--verify-workers` argument preserved
- Auto-discovery mode unchanged (just uses new builder internally)
- Result JSON format extended (new fields, existing fields unchanged)
