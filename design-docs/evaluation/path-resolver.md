# Path Resolver Design

## Overview

The path resolver module (`crsbench/evaluation/path_resolver.py`) provides functionality to parse `$REPO` and `$PROJECT` variables in harness file paths and resolve them to actual host filesystem paths for passing to CRS commands.

## Purpose

When running CRS via `oss-crs` or `oss-patch-crs` commands, harness source file paths can be provided to enable CRS to analyze harness code. Harness paths in `meta.yaml` use variables for flexibility:

- `$REPO/test/harness.c` - Path relative to cloned repository
- `$PROJECT/harness.c` - Path relative to OSS-Fuzz project directory

The path resolver translates these variables into concrete host paths that are passed as arguments to CRS commands via the `--harness-source` flag. The CRS implementation then decides how to handle this path (mount, copy, read, etc.).

## Use Cases

### 1. Bug Finding CRS
Pass harness source path to `oss-crs`:
```bash
oss-crs run ensemble-c json-c json_array_fuzzer \
  --output /tmp/output \
  --harness-source /host/repos/json-c/test/json_array_fuzzer.c
```

### 2. Patch Generation CRS
Pass harness source path to `oss-patch-crs`:
```bash
oss-patch-crs run multi-retrieval mock-c \
  --harness fuzz_process_input \
  --povs /tmp/povs \
  --harness-source /host/repos/mock-c/fuzzers/fuzz_process_input.c \
  --litellm-base https://api.litellm.com \
  --litellm-key sk-key
```

### 3. CRS Implementation Decision
The CRS implementation receives the host path and decides how to use it:
- Mount it into the container (via Docker -v)
- Copy it into the container during build
- Read it before launching the container
- Ignore it if not needed

## Architecture

### Module Structure

```
crsbench/evaluation/
├── path_resolver.py      # Core path resolution logic
├── crs_executor.py       # Uses path resolver
└── oss_patch_executor.py # Uses path resolver
```

### Integration Points

```
HarnessFile (meta.yaml)
    ↓ harness.path contains $REPO or $PROJECT
PathResolver
    ↓ integrates with
RepoManager (migration/repo_manager.py)
    ↓ provides repository location
PathResolver
    ↓ outputs
(host_path, container_path) tuple
    ↓ used by
CRSExecutor
    ↓ generates
Docker volume mount arguments
```

## Core Functions

### resolve_harness_path()

Resolve harness path with variables to actual host path.

**Signature:**
```python
def resolve_harness_path(
    harness_path: str,
    benchmark_dir: Path,
    repos_dir: Optional[Path] = None,
    project_dir: Optional[Path] = None
) -> Path:
    """
    Resolve harness path with $REPO/$PROJECT variables to host path.

    Args:
        harness_path: Path from meta.yaml (may contain $REPO or $PROJECT)
        benchmark_dir: Path to benchmark directory
        repos_dir: Repository cache directory (optional)
        project_dir: Explicit project directory override (optional)

    Returns:
        Resolved absolute path on host filesystem

    Raises:
        ValueError: If path format is invalid
        FileNotFoundError: If resolved path doesn't exist
        RepositoryError: If repository cloning fails
    """
```

**Resolution Logic:**

| Input Pattern | Resolution Strategy | Example |
|--------------|-------------------|---------|
| `$REPO/path` | Use repo_manager to get cloned repo path | `$REPO/test/harness.c` → `/repos/json-c/test/harness.c` |
| `$PROJECT/path` | Use benchmark directory as project root | `$PROJECT/fuzz.c` → `/benchmarks/json-c/fuzz.c` |
| `/absolute` | Return as-is (container path) | `/src/harness.c` → `/src/harness.c` |
| `./relative` | Resolve relative to benchmark dir | `./test/harness.c` → `/benchmarks/json-c/test/harness.c` |

**Implementation:**
```python
def resolve_harness_path(harness_path: str, benchmark_dir: Path,
                        repos_dir: Optional[Path] = None,
                        project_dir: Optional[Path] = None) -> Path:
    """Resolve harness path to host filesystem path."""
    harness_path = harness_path.strip()

    # $REPO variable resolution
    if harness_path.startswith('$REPO/'):
        relative_path = harness_path[6:]  # Remove "$REPO/"
        repo_path = _resolve_repo_path(benchmark_dir, repos_dir, project_dir)
        resolved = repo_path / relative_path

        if not resolved.exists():
            raise FileNotFoundError(
                f"Harness file not found: {resolved}\n"
                f"  Original path: {harness_path}\n"
                f"  Repository: {repo_path}"
            )
        return resolved.absolute()

    # $PROJECT variable resolution
    elif harness_path.startswith('$PROJECT/'):
        relative_path = harness_path[9:]  # Remove "$PROJECT/"
        project_path = project_dir if project_dir else benchmark_dir
        resolved = project_path / relative_path

        if not resolved.exists():
            raise FileNotFoundError(
                f"Harness file not found: {resolved}\n"
                f"  Original path: {harness_path}\n"
                f"  Project dir: {project_path}"
            )
        return resolved.absolute()

    # Absolute paths (assume container paths, return as-is)
    elif harness_path.startswith('/'):
        return Path(harness_path)

    # Relative paths
    elif harness_path.startswith('./'):
        relative_path = harness_path[2:]  # Remove "./"
        resolved = benchmark_dir / relative_path

        if not resolved.exists():
            raise FileNotFoundError(
                f"Harness file not found: {resolved}\n"
                f"  Original path: {harness_path}\n"
                f"  Benchmark dir: {benchmark_dir}"
            )
        return resolved.absolute()

    else:
        raise ValueError(
            f"Invalid harness path format: {harness_path}\n"
            f"  Expected: $REPO/..., $PROJECT/..., /absolute, or ./relative"
        )
```

### get_harness_source_path()

Resolve harness path for passing to CRS commands via `--harness-source`.

**Signature:**
```python
def get_harness_source_path(
    harness: HarnessFile,
    benchmark_dir: Path,
    repos_dir: Optional[Path] = None,
    project_dir: Optional[Path] = None
) -> Optional[Path]:
    """
    Resolve harness source path for CRS command argument.

    Args:
        harness: HarnessFile object from meta.yaml
        benchmark_dir: Path to benchmark directory
        repos_dir: Repository cache directory (optional)
        project_dir: Explicit project directory (optional)

    Returns:
        Resolved host path, or None if resolution fails

    Example:
        harness_path = get_harness_source_path(harness, benchmark_dir)
        if harness_path:
            cmd.extend(["--harness-source", str(harness_path)])
    """
```

**Implementation:**
```python
def get_harness_source_path(harness: HarnessFile, benchmark_dir: Path,
                            repos_dir: Optional[Path] = None,
                            project_dir: Optional[Path] = None) -> Optional[Path]:
    """Resolve harness source path for CRS argument."""
    try:
        return resolve_harness_path(
            harness.path, benchmark_dir, repos_dir, project_dir
        )
    except Exception as e:
        logger.warning(f"Could not resolve harness source path: {e}")
        return None
```

**Note:** This function replaces the previous `get_mount_mapping()` function, since CRSBench doesn't mount harness files directly. Instead, it passes the path to CRS commands, and the CRS implementation decides how to handle it.

### _resolve_repo_path() (Internal Helper)

Get repository path using repo_manager.

**Signature:**
```python
def _resolve_repo_path(
    benchmark_dir: Path,
    repos_dir: Optional[Path],
    project_dir: Optional[Path]
) -> Path:
    """
    Get repository path for $REPO resolution.

    Priority:
      1. Explicit project_dir if provided
      2. Clone repository using repo_manager

    Args:
        benchmark_dir: Benchmark directory
        repos_dir: Repository cache directory
        project_dir: Explicit project directory override

    Returns:
        Path to repository root

    Raises:
        RepositoryError: If repository cannot be obtained
    """
```

**Implementation:**
```python
from crsbench.migration.repo_manager import ensure_project_repository

def _resolve_repo_path(benchmark_dir: Path, repos_dir: Optional[Path],
                      project_dir: Optional[Path]) -> Path:
    """Resolve repository path using repo_manager."""
    # Use explicit project_dir if provided
    if project_dir:
        if not project_dir.exists():
            raise FileNotFoundError(f"Project directory not found: {project_dir}")
        return project_dir

    # Clone/find repository using repo_manager
    repo_path = ensure_project_repository(
        benchmark_dir=str(benchmark_dir),
        repos_dir=str(repos_dir) if repos_dir else None,
        verbose=False
    )

    if not repo_path:
        raise RepositoryError(
            f"Failed to obtain repository for benchmark: {benchmark_dir}"
        )

    return Path(repo_path)
```

## Error Handling

### Error Types

**ValueError**: Invalid path format
```python
# Bad: test/harness.c (missing ./ prefix)
resolve_harness_path("test/harness.c", benchmark_dir)
# Raises: ValueError: Invalid harness path format
```

**FileNotFoundError**: Resolved path doesn't exist
```python
# File missing after resolution
resolve_harness_path("$REPO/nonexistent.c", benchmark_dir)
# Raises: FileNotFoundError: Harness file not found: /repos/json-c/nonexistent.c
```

**RepositoryError**: Cannot obtain repository
```python
# Repository cloning fails
resolve_harness_path("$REPO/test.c", benchmark_dir)
# Raises: RepositoryError: Failed to obtain repository for benchmark
```

### Error Messages

Provide detailed context in error messages:
```python
raise FileNotFoundError(
    f"Harness file not found: {resolved}\n"
    f"  Original path: {harness_path}\n"
    f"  Repository: {repo_path}\n"
    f"  \n"
    f"  Possible causes:\n"
    f"    - File doesn't exist in repository\n"
    f"    - Wrong commit checked out\n"
    f"    - Path typo in meta.yaml"
)
```

### Graceful Degradation

CRS executors should handle resolution failures gracefully:
```python
try:
    host_path, container_path = get_mount_mapping(harness, benchmark_dir)
    docker_args.extend(["-v", f"{host_path}:{container_path}:ro"])
    logger.info(f"Mounting harness: {host_path} → {container_path}")
except Exception as e:
    logger.warning(f"Could not mount harness {harness.name}: {e}")
    logger.warning("CRS will run without harness source code access")
    # Continue execution without mount
```

## Integration with CRS Executors

### Bug Finding CRS Example

```python
from crsbench.evaluation.path_resolver import get_harness_source_path

class CRSBugFindingExecutor(CRSExecutor):
    def run_crs(self, benchmark_path: Path, harness: HarnessFile, ...) -> CRSResult:
        """Run CRS with optional harness source path."""
        # Build base command
        cmd = [
            "oss-crs", "run",
            self.crs_config_name,
            project_name,
            harness.name,
            "--output", str(trial_output_dir)
        ]

        # Add optional harness source path
        harness_source = get_harness_source_path(
            harness, benchmark_path, self.repos_dir
        )
        if harness_source:
            cmd.extend(["--harness-source", str(harness_source)])
            logger.info(f"Harness source: {harness_source}")
        else:
            logger.warning("Running without harness source code")

        # Add hints if available
        if hints_dir:
            cmd.extend(["--hints", str(hints_dir)])

        # Execute
        result = subprocess.run(cmd, ...)
        return self._parse_result(result)
```

### Patch Generation CRS Example

```python
from crsbench.evaluation.path_resolver import get_harness_source_path

class CRSPatchExecutor(CRSExecutor):
    def run_crs(self, benchmark_path: Path, harness: HarnessFile, ...) -> CRSResult:
        """Run patch CRS with optional harness source path."""
        # Build base command
        cmd = [
            "oss-patch-crs", "run",
            self.crs_config_name,
            project_name,
            "--harness", harness.name,
            "--povs", str(povs_dir),
            "--output", str(trial_output_dir),
            "--litellm-base", litellm_base_url,
            "--litellm-key", litellm_key
        ]

        # Add optional harness source path
        harness_source = get_harness_source_path(
            harness, benchmark_path, self.repos_dir
        )
        if harness_source:
            cmd.extend(["--harness-source", str(harness_source)])
            logger.info(f"Harness source: {harness_source}")

        # Add hints if available
        if hints_dir:
            cmd.extend(["--hints", str(hints_dir)])

        # Execute
        result = subprocess.run(cmd, ...)
        return self._parse_result(result)
```

## Testing Strategy

### Unit Tests

**Test Categories:**
1. Variable resolution (`$REPO`, `$PROJECT`)
2. Path format handling (absolute, relative)
3. Mount mapping generation
4. Error handling
5. Integration with repo_manager

**Example Test:**
```python
def test_resolve_repo_variable(tmp_path):
    """Test $REPO variable resolution."""
    # Setup
    benchmark = tmp_path / "benchmarks" / "json-c"
    benchmark.mkdir(parents=True)

    # Create project.yaml with repo info
    (benchmark / "project.yaml").write_text("""
main_repo: https://github.com/json-c/json-c.git
    """)

    # Create meta.yaml with commit
    aixcc = benchmark / ".aixcc"
    aixcc.mkdir()
    (aixcc / "meta.yaml").write_text("""
full_mode:
  base_commit: abc123
harness_files:
  - name: test
    path: $REPO/test/harness.c
    """)

    # Mock cloned repository
    repos_dir = tmp_path / "repos"
    repo = repos_dir / "json-c"
    repo.mkdir(parents=True)
    harness_file = repo / "test" / "harness.c"
    harness_file.parent.mkdir(parents=True)
    harness_file.write_text("// harness code")

    # Test resolution
    resolved = resolve_harness_path(
        "$REPO/test/harness.c",
        benchmark_dir=benchmark,
        repos_dir=repos_dir
    )

    assert resolved == harness_file
    assert resolved.exists()
```

### Integration Tests

**Test with repo_manager:**
```python
def test_integration_with_repo_cloning(tmp_path):
    """Test resolution triggers repository cloning."""
    # Create benchmark config
    benchmark = create_test_benchmark(tmp_path)

    # Resolve harness (should trigger clone)
    resolved = resolve_harness_path(
        "$REPO/test/harness.c",
        benchmark_dir=benchmark,
        repos_dir=tmp_path / "repos"
    )

    # Verify repository was cloned
    assert (tmp_path / "repos" / "json-c").exists()
    assert resolved.exists()
```

### Error Handling Tests

```python
def test_missing_harness_file():
    """Test error when harness file doesn't exist."""
    with pytest.raises(FileNotFoundError) as exc_info:
        resolve_harness_path("$REPO/missing.c", benchmark_dir)

    assert "Harness file not found" in str(exc_info.value)
    assert "missing.c" in str(exc_info.value)

def test_invalid_path_format():
    """Test error on invalid path format."""
    with pytest.raises(ValueError) as exc_info:
        resolve_harness_path("test/harness.c", benchmark_dir)

    assert "Invalid harness path format" in str(exc_info.value)
```

## Environment Variables

**PROJECT_REPOS_DIR** (inherited from repo_manager):
- Default repository cache location
- Used when `repos_dir` parameter not provided

## Performance Considerations

### Repository Cloning
- First resolution may be slow (git clone)
- Subsequent resolutions are fast (cached)
- repo_manager handles caching internally

### Path Resolution
- Minimal overhead (string parsing + Path operations)
- No network I/O after initial clone
- Typical resolution: <1ms

## Future Enhancements

### 1. Path Validation Cache
```python
_resolution_cache: Dict[str, Path] = {}

def resolve_harness_path(harness_path: str, ...) -> Path:
    cache_key = f"{harness_path}:{benchmark_dir}"
    if cache_key in _resolution_cache:
        return _resolution_cache[cache_key]

    resolved = _do_resolution(harness_path, ...)
    _resolution_cache[cache_key] = resolved
    return resolved
```

### 2. Pre-validation
```python
def validate_all_harness_paths(config: BenchmarkConfig,
                               benchmark_dir: Path) -> List[str]:
    """Validate all harness paths before execution."""
    errors = []
    for harness in config.harness_files:
        try:
            resolve_harness_path(harness.path, benchmark_dir)
        except Exception as e:
            errors.append(f"{harness.name}: {e}")
    return errors
```

### 3. Batch Resolution
```python
def resolve_all_harnesses(config: BenchmarkConfig,
                         benchmark_dir: Path) -> Dict[str, Path]:
    """Resolve all harness paths at once."""
    resolved = {}
    for harness in config.harness_files:
        try:
            resolved[harness.name] = resolve_harness_path(
                harness.path, benchmark_dir
            )
        except Exception as e:
            logger.warning(f"Could not resolve {harness.name}: {e}")
    return resolved
```

### 4. Mount Options Configuration
```python
def get_mount_mapping(harness: HarnessFile, ...,
                     mount_options: str = "ro") -> tuple[Path, str, str]:
    """Generate mount mapping with custom options."""
    host_path, container_path = ...
    return (host_path, container_path, mount_options)

# Usage: -v {host_path}:{container_path}:{mount_options}
```

## Related Documentation

- **Validation Module**: `design-docs/validation/validation.md` - Path validation in meta.yaml
- **Repository Manager**: `design-docs/migration/repo-manager.md` - Repository cloning
- **CRS Executors**: `design-docs/evaluation/crs-executors.md` - Integration points
- **Benchmark Spec**: `docs/benchmark-spec.md` - Harness path specification
