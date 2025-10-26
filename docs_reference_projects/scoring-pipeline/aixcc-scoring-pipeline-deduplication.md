# AIxCC Scoring Pipeline - POV and Patch Deduplication

## Overview

The AIxCC (AI Cyber Challenge) scoring pipeline implements a plugin-based deduplication system for both POVs (Proof of Vulnerabilities) and patches. This prevents duplicate submissions from receiving credit multiple times in the competition.

## Architecture

### Plugin-Based Design

Both POV and patch deduplication use an abstract plugin architecture:

- **Abstract base classes**: `PovAbstractDeduplicator` and `PatchAbstractDeduplicator`
- **Plugin loader**: `DedupPluginLoader` dynamically loads plugins based on environment variables
- **Environment configuration**:
  - `POV_DEDUP_PLUGINS`: Comma-separated list or "DEFAULT"
  - `PATCH_DEDUP_PLUGINS`: Comma-separated list or "DEFAULT"

### Execution Model

**POV Deduplication** (scoring-pipeline/scoring/deduplicator/pov/pov_deduplicator.py):
- Compares all POV pairs using `permutations(pov_list, 2)`
- **Parallelized**: Uses `ThreadPoolExecutor` with `MAX_CHILD_TASK_THREADS` workers
- **Chunked processing**: Processes in chunks of 10,000 pairs to manage memory
- **Pre-computed map**: Creates numpy array mapping (patch_id, pov_id) → ScantronStatus
  - Avoids redundant Scantron API calls during deduplication
  - Map size: `(patch_count + 1) × (pov_count + 1)`
- **Result**: Yields `PoVDuplication` records with reason

**Patch Deduplication** (scoring-pipeline/scoring/deduplicator/patch/patch_deduplicator.py):
- Compares consecutive patch pairs using `pairwise(patch_list)`
- **Sequential**: No parallelization (simpler than POV dedup)
- **Scope constraint**: Only deduplicates patches within same team and task
- **Result**: Returns list of `PatchDuplication` records

## POV Deduplication Plugins

### 1. PoVIsCopy (pov_iscopy.py)

**Purpose**: Detect exact copies of POVs

**Method**: Direct equality check (`pov_a == pov_b`)

**Reason**: `PoVDuplicationReasons.COPY`

**Use case**: Catch accidental resubmissions or identical POVs

### 2. PoVIsMatch (pov_ismatch.py)

**Purpose**: Detect POVs with identical key fields

**Method**: Checks equality of:
- `task_uuid` - Challenge task identifier
- `testcase_sha256` - SHA256 hash of POV testcase file
- `sanitizer` - Sanitizer used (ASAN, MSAN, UBSAN, etc.)
- `build_architecture` - Architecture (x86_64, etc.)
- `fuzzer_name` - Fuzzer that generated the POV

**Reason**: `PoVDuplicationReasons.EXACT_MATCH`

**Use case**: Different submission UUIDs but same underlying vulnerability trigger

### 3. PoVPatchValueAddedDeduplicator (pov_patch_deduplicator.py)

**Purpose**: Detect POVs fixed by the same patch (indicating same vulnerability)

**Method**:
1. Check pre-computed `completed_jobs_map[patch_id, pov_a_id]` and `[patch_id, pov_b_id]`
2. If both show `ScantronStatus.PASSED`, POVs are duplicates
3. If inconclusive, submit new Scantron evaluation jobs
4. Return true if ANY patch fixes both POVs

**Reason**: `PoVDuplicationReasons.PATCH_MATCH`

**Use case**: Two different POV files that trigger the same underlying vulnerability

**Performance optimization**:
- Uses pre-computed numpy map to avoid Scantron calls
- Only falls back to Scantron API if no cached result exists
- Implements retry logic (up to `PATCH_BUILD_RETRIES` times) for errored jobs

**Key insight**: If a patch fixes both POVs, they likely exploit the same bug

## Patch Deduplication Plugins

### 1. PatchIsCopy (patch_iscopy.py)

**Purpose**: Detect exact patch copies

**Method**:
1. Compare all model fields except `id` using `model_dump(exclude={"id"})`
2. Compare archive metadata `object_name` (S3 storage key)

**Reason**: `PatchDuplicationReasons.COPY`

**Use case**: Resubmitted identical patches

### 2. PatchIsMatch (patch_ismatch.py)

**Purpose**: Detect patches with matching key fields

**Method**: Checks equality of:
- `team_id` - AIxCC team identifier
- `task_id` - Challenge task identifier
- `archive_metadata.object_name` - S3 object key for patch file

**Reason**: `PatchDuplicationReasons.EXACT_MATCH`

**Use case**: Same patch file submitted under different UUIDs

### 3. PatchVulnDeduplicator (patch_vuln_deduplicator.py)

**Purpose**: Detect patches that fix the same vulnerabilities (no added value)

**Method**:
1. Extract vulnerability sets from `patch.vulnerability_mappings`
2. Check if `a_vulns ⊆ b_vulns` OR `b_vulns ⊆ a_vulns` (one is subset of other)
3. Reject if one set is empty and the other is not

**Reason**: `PatchDuplicationReasons.VULN_MATCH`

**Use case**: A patch that only fixes vulnerabilities already fixed by a previous patch from the same team

**Key insight**: If patch B only fixes vulnerabilities that patch A already fixed, patch B adds no value to the team's score

## Data Structures

### PoVDuplication Table

```python
pov_id: int              # Source POV
duplicates: int          # Duplicate POV
duplicate_according_to: PoVDuplicationReasons
```

### PatchDuplication Table

```python
patch_id: int            # Source patch
duplicates: int          # Duplicate patch
duplicate_according_to: PatchDuplicationReasons
```

### Deduplication Reasons (Enums)

**POV**:
- `COPY`: Exact copy
- `EXACT_MATCH`: Key fields match
- `PATCH_MATCH`: Fixed by same patch

**Patch**:
- `COPY`: Exact copy
- `EXACT_MATCH`: Key fields match
- `VULN_MATCH`: Fixes same vulnerabilities

## Plugin Loader Configuration

Default plugins (from `plugin_loader.py:98-109`):

**Patches**:
1. PatchIsCopy
2. PatchIsMatch
3. PatchVulnDeduplicator

**POVs**:
1. PoVIsCopy
2. PoVIsMatch
3. PoVPatchValueAddedDeduplicator

Override via environment variables:
```bash
# Use only specific plugins
POV_DEDUP_PLUGINS="PoVIsCopy,PoVIsMatch"
PATCH_DEDUP_PLUGINS="PatchIsCopy,PatchVulnDeduplicator"

# Use all default plugins
POV_DEDUP_PLUGINS="DEFAULT"
```

## Performance Considerations

### POV Deduplication

**Parallelization strategy**:
- Main thread: Chunks POV pairs into 10k chunks
- Worker threads: Process chunks in parallel
- Workers: `MAX_CHILD_TASK_THREADS` (default: CPU count)

**Memory optimization**:
- Pre-compute patch/POV evaluation map as numpy array
- Avoids repeated database queries and Scantron API calls
- Trade-off: `O(patches × POVs)` memory for `O(1)` lookup time

**Time complexity**:
- Worst case: `O(n²)` for n POVs (all permutations)
- Mitigated by parallel processing and caching

### Patch Deduplication

**Simplification**:
- Uses `pairwise()` instead of full permutations
- Assumes patches are temporally ordered
- Only checks consecutive pairs: `O(n)` instead of `O(n²)`

**Trade-off**: May miss non-adjacent duplicates, but acceptable since:
- Patches are processed chronologically
- VulnDeduplicator catches semantic duplicates regardless of position

## Key Design Insights

1. **Plugin abstraction**: Easy to add new deduplication strategies without modifying core code

2. **Gauntlet pattern**: POVs/patches run through all deduplicators; first match wins

3. **Pre-computation**: Building the Scantron job map upfront saves massive time during POV dedup

4. **Scoping constraints**: Only compare within same task (POVs) or same team+task (patches)

5. **Semantic vs syntactic**: Includes both file-level (IsCopy, IsMatch) and semantic (PatchValueAdded, VulnMatch) deduplicators

6. **Asymmetric complexity**: POV dedup is much more expensive (O(n²), parallelized) vs patch dedup (O(n), sequential)

## Integration with AIxCC Scoring Pipeline

Deduplication runs as dedicated plugin tasks in the pipeline:
- `VulnerabilityBucketing`: Groups POVs using deduplication results
- `PatchDeduplication`: Identifies duplicate patches before evaluation

Duplicate items are:
- Tracked in database (`PoVDuplication`, `PatchDuplication` tables)
- Excluded from scoring (only first submission counts)
- Visible to competition organizers for audit purposes

## Applicability to CRSBench

**Differences to consider**:

1. **AIxCC uses Scantron API**: CRSBench uses local evaluation via OSS-Fuzz interface
   - PoVPatchValueAddedDeduplicator needs adaptation for local execution
   - No pre-computed Scantron job map in CRSBench

2. **AIxCC is competition-focused**: CRSBench is benchmark-focused
   - CRSBench may not need team-based scoping
   - Different deduplication semantics for reproducibility vs scoring

3. **Storage differences**: AIxCC uses S3, CRSBench uses local filesystem
   - Archive metadata comparison needs adjustment

**Reusable concepts**:
- Plugin architecture for extensible deduplication
- Separate syntactic (IsCopy, IsMatch) and semantic (value-added) deduplication
- Parallelization strategy for large-scale POV comparison
- Pre-computation of evaluation results to avoid redundant work

## Extensibility

To add a new deduplicator:

1. Create new class inheriting from `PovAbstractDeduplicator` or `PatchAbstractDeduplicator`
2. Implement `is_duplicate(self, a, b, **kwargs) -> bool`
3. Set `duplication_reason` to appropriate enum value
4. Register in `DedupPluginLoader.POV_PLUGINS` or `PATCH_PLUGINS`
5. Add to default list or specify in environment variable
