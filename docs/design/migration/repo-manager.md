# Repository Manager Design

## Overview

The repository manager (`crsbench/migration/repo_manager.py`) provides automatic cloning and checkout functionality for project repositories needed during migration and test generation workflows.

## Purpose

When generating test.sh or migrating benchmarks, we need access to the actual project source code. The repository manager:

1. **Reads repository information** from benchmark configuration files
2. **Automatically clones repositories** if they don't exist locally
3. **Checks out specific commits** for reproducible testing
4. **Manages repository cache** to avoid redundant clones

## Architecture

### Component Relationships

```
Migration Tools (generate_test_sh.py, etc.)
    ↓ calls
repo_manager.py
    ↓ reads config from
benchmark/.aixcc/meta.yaml + project.yaml
    ↓ clones to
Repositories Cache (PROJECT_REPOS_DIR)
    ↓ used by
Test Generation / Migration
```

### Key Functions

#### 1. `get_repo_info_from_benchmark(benchmark_dir)`

Extracts repository information from benchmark configuration files.

**Input:**
- `benchmark_dir`: Path to benchmark directory (e.g., `benchmarks/json-c`)

**Process:**
1. Read `project.yaml` for `main_repo` URL
2. Read `.aixcc/meta.yaml` for commit information
3. Extract `base_commit` from `delta_mode` or `full_mode`
4. Extract `ref_commit` if in delta mode

**Output:**
```python
{
    "repo_url": "https://github.com/json-c/json-c.git",
    "base_commit": "abc123...",
    "ref_commit": "def456..."  # Optional, only in delta mode
}
```

**Configuration File Structure:**

**project.yaml:**
```yaml
homepage: https://github.com/json-c/json-c
language: c
main_repo: https://github.com/json-c/json-c.git
```

**meta.yaml (delta mode):**
```yaml
delta_mode:
  base_commit: abc123...
  ref_commit: def456...
```

**meta.yaml (full mode):**
```yaml
full_mode:
  base_commit: abc123...
```

#### 2. `derive_repo_name_from_url(repo_url)`

Derives a directory name from a git repository URL.

**Examples:**
```python
derive_repo_name_from_url("git@github.com:Team-Atlanta/cp-c-curl.git")
# → "cp-c-curl"

derive_repo_name_from_url("https://github.com/curl/curl.git")
# → "curl"
```

**Algorithm:**
1. Extract last path component from URL
2. Remove `.git` extension if present
3. Return cleaned name

#### 3. `clone_repository(repo_url, target_dir, commit, verbose)`

Clones a git repository and optionally checks out a specific commit.

**Workflow:**

```
Check if target_dir exists
    ↓ no
Create parent directories
    ↓
Execute: git clone <repo_url> <target_dir>
    ↓
Check clone success
    ↓
If commit specified:
    Execute: git checkout <commit>
    ↓
Return success status
```

**Smart Behavior:**
- If directory exists and is a git repo → Skip clone (success)
- If directory exists but not a git repo → Error
- Clone timeout: 5 minutes
- Checkout timeout: 60 seconds
- If checkout fails, still return success (clone succeeded)

**Usage:**
```python
success = clone_repository(
    repo_url="https://github.com/json-c/json-c.git",
    target_dir="/repos/json-c",
    commit="abc123...",
    verbose=True
)
```

#### 4. `ensure_project_repository(benchmark_dir, repos_dir, project_dir, verbose)`

High-level function to ensure a project repository exists, cloning if necessary.

**Workflow:**

```
project_dir specified?
    ↓ yes
    Directory exists? → Return path
    ↓ no
Get repo info from benchmark
    ↓
Determine target directory:
    - Use project_dir if specified
    - Otherwise: repos_dir/<derived-name>
    ↓
Directory exists?
    ↓ no
Clone repository to target
    ↓
Return path
```

**Arguments:**
- `benchmark_dir`: Path to benchmark (e.g., `benchmarks/json-c`)
- `repos_dir`: Cache directory for clones (default: `$PROJECT_REPOS_DIR`)
- `project_dir`: Explicit path to use (optional, overrides auto-detection)
- `verbose`: Enable detailed logging

**Environment Variables:**
- `PROJECT_REPOS_DIR`: Default repository cache location
  - Default fallback: `/home/acorn421/work/team-atlanta/afc-repos`
  - Override with environment variable for custom cache location

**Return Value:**
- Path to project directory if successful
- `None` if failed

#### 5. `find_or_clone_project(benchmark_name, benchmarks_root, repos_dir, project_dir, verbose)`

Convenience wrapper for common use case.

**Usage:**
```python
project_path = find_or_clone_project(
    benchmark_name="json-c",
    benchmarks_root="benchmarks",
    repos_dir="/tmp/repos",
    verbose=True
)
```

Internally calls `ensure_project_repository` with constructed benchmark path.

## Configuration

### Environment Variables

**`PROJECT_REPOS_DIR`** (optional)
- **Purpose**: Default location for cloning project repositories
- **Default**: `/home/acorn421/work/team-atlanta/afc-repos`
- **Usage**: Set to customize repository cache location

```bash
export PROJECT_REPOS_DIR=/custom/path/to/repos
```

### Benchmark Configuration Requirements

Repository manager expects specific files in benchmark directories:

**Required Files:**
1. `project.yaml` - Must contain `main_repo` field
2. `.aixcc/meta.yaml` - Must contain commit information

**meta.yaml Structure:**

For delta mode benchmarks:
```yaml
delta_mode:
  base_commit: <commit-hash>
  ref_commit: <commit-hash>
```

For full mode benchmarks:
```yaml
full_mode:
  base_commit: <commit-hash>
```

## Integration with Migration Tools

### test.sh Generator

**Usage in `generate_test_sh.py`:**

```python
from crsbench.migration.repo_manager import ensure_project_repository

# Ensure project repository exists
project_dir = ensure_project_repository(
    benchmark_dir=benchmark_path,
    project_dir=args.project_dir,  # Optional override
    verbose=args.verbose
)

if not project_dir:
    print("❌ Failed to clone project repository")
    exit(1)

# Now use project_dir for test.sh generation
```

The test.sh generator needs access to project source code to:
- Analyze project structure
- Extract build commands
- Generate harness test scripts

### Migration Script

Repository manager can be used in migration workflows to:
- Validate repository URLs are accessible
- Pre-clone repositories for batch migration
- Verify commit hashes exist in repositories

## Error Handling

### Error Types

**1. Configuration Errors:**
- `FileNotFoundError`: Missing `project.yaml` or `meta.yaml`
- `ValueError`: Missing required fields (`main_repo`, commits)

**2. Clone Errors:**
- Directory exists but not a git repo
- Git clone failed (network, permissions, invalid URL)
- Clone timeout (>5 minutes)

**3. Checkout Errors:**
- Invalid commit hash
- Checkout timeout (>60 seconds)
- Note: Checkout failure doesn't fail the operation (clone succeeded)

### Error Recovery

**Strategy 1: Partial Success**
```python
success = clone_repository(...)
if success:
    # Clone succeeded, even if checkout failed
    # Can still use repository at HEAD
```

**Strategy 2: Graceful Degradation**
```python
project_dir = ensure_project_repository(...)
if not project_dir:
    # Fall back to alternative source
    # Or fail with clear error message
```

## Logging

### Log Levels

**INFO** (verbose mode):
- Directory existence checks
- Clone operations
- Checkout operations
- Success messages

**ERROR** (always):
- Configuration errors
- Clone failures
- Missing directories

### Log Format

Uses emojis for visual clarity:
- ✅ Success
- ❌ Error
- ⚠️  Warning
- 🔄 In progress
- 📦 Repository operation

**Example Log Output:**
```
📦 Repository not found, cloning...
🔄 Cloning https://github.com/json-c/json-c.git to /repos/json-c...
✅ Successfully cloned repository
🔄 Checking out commit abc123...
✅ Checked out commit abc123
```

## Design Decisions

### Why Repository Cache?

**Problem:** Migration tools need project source code, but:
- Cloning is slow (~minutes per repo)
- Network failures can occur
- Same repo may be needed multiple times

**Solution:** Repository cache with smart reuse
- Clone once, reuse many times
- Check if directory exists before cloning
- Validate it's a git repo before reusing

### Why Allow Explicit project_dir?

**Use Case 1:** Developer has pre-cloned repos
```python
ensure_project_repository(
    benchmark_dir="benchmarks/json-c",
    project_dir="/my/existing/json-c"
)
```

**Use Case 2:** Custom organization
```python
# Organization prefers different structure
ensure_project_repository(
    benchmark_dir="benchmarks/curl",
    repos_dir="/company/git-cache"
)
```

### Why Separate Info Extraction from Cloning?

**Separation of Concerns:**

`get_repo_info_from_benchmark()` - Pure function, reads config
- Easy to test
- No side effects
- Can validate config without network access

`clone_repository()` - Side effect, modifies filesystem
- Requires network
- Requires disk space
- May fail due to external factors

**Benefit:** Can validate configuration before attempting clone

### Why Continue on Checkout Failure?

**Scenario:**
```
Clone succeeds → Repository at HEAD
Checkout fails → Can't get specific commit
```

**Decision:** Return success (clone succeeded)

**Rationale:**
1. Repository is still usable at HEAD
2. Checkout failure often means:
   - Commit hasn't been pushed yet (development)
   - Commit hash is incorrect (config error)
3. Caller can decide how to handle
4. Test generation might work with HEAD anyway

## Usage Examples

### Example 1: Basic Usage

```python
from crsbench.migration.repo_manager import find_or_clone_project

# Find or clone json-c project
project_dir = find_or_clone_project(
    benchmark_name="json-c",
    benchmarks_root="benchmarks",
    verbose=True
)

if project_dir:
    print(f"Project ready at: {project_dir}")
else:
    print("Failed to obtain project")
```

### Example 2: Custom Cache Location

```python
import os
from crsbench.migration.repo_manager import ensure_project_repository

# Set custom cache
os.environ['PROJECT_REPOS_DIR'] = '/mnt/ssd/git-cache'

# Clone will use custom cache
project_dir = ensure_project_repository(
    benchmark_dir="benchmarks/curl",
    verbose=True
)
```

### Example 3: Using Existing Clone

```python
from crsbench.migration.repo_manager import ensure_project_repository

# Developer has pre-cloned repo
project_dir = ensure_project_repository(
    benchmark_dir="benchmarks/json-c",
    project_dir="/home/user/projects/json-c",
    verbose=True
)

# Output: ✅ Using existing project directory: /home/user/projects/json-c
```

### Example 4: Batch Migration Preparation

```python
from crsbench.migration.repo_manager import ensure_project_repository
from pathlib import Path

benchmarks_dir = Path("benchmarks")
repos_cache = Path("/tmp/migration-cache")

# Pre-clone all repositories
for benchmark in benchmarks_dir.iterdir():
    if benchmark.is_dir():
        print(f"Ensuring {benchmark.name}...")

        project_dir = ensure_project_repository(
            benchmark_dir=str(benchmark),
            repos_dir=str(repos_cache),
            verbose=True
        )

        if project_dir:
            print(f"✅ {benchmark.name} ready")
        else:
            print(f"❌ {benchmark.name} failed")
```

### Example 5: Configuration Validation

```python
from crsbench.migration.repo_manager import get_repo_info_from_benchmark

try:
    repo_info = get_repo_info_from_benchmark("benchmarks/json-c")

    print(f"Repository: {repo_info['repo_url']}")
    print(f"Base commit: {repo_info['base_commit']}")

    if repo_info['ref_commit']:
        print(f"Ref commit: {repo_info['ref_commit']} (delta mode)")
    else:
        print("Full mode (no ref commit)")

except FileNotFoundError as e:
    print(f"Configuration missing: {e}")
except ValueError as e:
    print(f"Configuration invalid: {e}")
```

## Testing

### Unit Tests

**Test get_repo_info_from_benchmark:**
```python
def test_get_repo_info_delta_mode(tmp_path):
    # Create mock benchmark
    benchmark = tmp_path / "test-bench"
    benchmark.mkdir()

    # Write project.yaml
    (benchmark / "project.yaml").write_text("""
main_repo: https://github.com/test/repo.git
    """)

    # Write meta.yaml
    aixcc = benchmark / ".aixcc"
    aixcc.mkdir()
    (aixcc / "meta.yaml").write_text("""
delta_mode:
  base_commit: abc123
  ref_commit: def456
    """)

    # Test extraction
    info = get_repo_info_from_benchmark(str(benchmark))

    assert info["repo_url"] == "https://github.com/test/repo.git"
    assert info["base_commit"] == "abc123"
    assert info["ref_commit"] == "def456"
```

**Test derive_repo_name_from_url:**
```python
def test_derive_repo_name():
    assert derive_repo_name_from_url("https://github.com/curl/curl.git") == "curl"
    assert derive_repo_name_from_url("git@github.com:user/repo.git") == "repo"
    assert derive_repo_name_from_url("https://github.com/org/project") == "project"
```

**Test clone_repository:**
```python
def test_clone_existing_repo(tmp_path):
    # Create fake git repo
    repo = tmp_path / "existing"
    repo.mkdir()
    (repo / ".git").mkdir()

    # Should detect existing repo
    result = clone_repository("dummy-url", str(repo), verbose=False)

    assert result is True
```

### Integration Tests

**Test with real repository:**
```python
@pytest.mark.integration
def test_clone_real_repository(tmp_path):
    # Clone small public repo
    target = tmp_path / "test-repo"

    success = clone_repository(
        repo_url="https://github.com/madler/zlib.git",
        target_dir=str(target),
        commit=None,
        verbose=False
    )

    assert success
    assert target.exists()
    assert (target / ".git").exists()
```

## Future Enhancements

### Potential Improvements

**1. Parallel Cloning:**
```python
def clone_multiple_repositories(repo_infos, repos_dir, max_workers=4):
    """Clone multiple repositories in parallel."""
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(clone_repository, info['url'], ...)
            for info in repo_infos
        ]
        return [f.result() for f in futures]
```

**2. Clone Progress Tracking:**
```python
def clone_repository_with_progress(repo_url, target_dir, progress_callback):
    """Clone with progress updates via callback."""
    # Use git clone --progress and parse output
```

**3. Repository Validation:**
```python
def validate_repository(project_dir, expected_commit):
    """Verify repository is at expected commit."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_dir,
        capture_output=True
    )
    current_commit = result.stdout.decode().strip()
    return current_commit == expected_commit
```

**4. Shallow Clone Support:**
```python
def clone_repository_shallow(repo_url, target_dir, depth=1):
    """Shallow clone for faster downloads."""
    cmd = ["git", "clone", "--depth", str(depth), repo_url, target_dir]
    # Useful when only need specific commit
```

**5. Automatic Retry:**
```python
def clone_repository_with_retry(repo_url, target_dir, max_retries=3):
    """Clone with automatic retry on network failures."""
    for attempt in range(max_retries):
        try:
            return clone_repository(repo_url, target_dir)
        except NetworkError:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)  # Exponential backoff
            else:
                raise
```

## Related Documentation

- **test.sh Generator**: [docs/design/migration/test-sh-generator.md](./test-sh-generator.md)
- **Migration Script**: [docs/design/migration/migration-atlanta-to-rfc.md](./migration-atlanta-to-rfc.md)
- **Benchmark Format**: [docs/benchmark-spec.md](../../benchmark-spec.md)
