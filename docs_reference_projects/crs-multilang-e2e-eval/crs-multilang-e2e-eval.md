# CRS-multilang End-to-End Evaluation System

**Source**: `claude_reference_projects/crs-multilang-e2e-eval/`

## Overview

A comprehensive evaluation system for running CRS-multilang fuzzing experiments across multiple targets with various input generation strategies. This system provides automated experiment orchestration, dynamic CPU scheduling, resource monitoring, and web-based reporting for large-scale fuzzing campaigns.

## Key Features

- **Multi-target support**: C, Java/JVM, C++ targets
- **Multiple input generation strategies**: given_fuzzer, mlla, testlang, concolic, dict_input_gen
- **Dynamic CPU scheduling**: Automatic resource allocation across experiments
- **Comprehensive monitoring**: CPU usage, Docker stats, LiteLLM tracking
- **Web interface**: Advanced reporting with multi-date support and authentication
- **Reproducibility**: Git metadata tracking for all experiments

## Architecture

### Core Components

1. **run_eval.py** (~1275 lines)
   - Main evaluation orchestrator
   - Dynamic CPU scheduling with slot management
   - Job queue with target collision prevention
   - Phase-based execution: config setup → metadata collection → image building → job scheduling
   - Graceful cleanup with signal handling

2. **experiments.py** (~1624 lines)
   - Experiment discovery and analysis logic
   - Step-by-step approach:
     - Fetch config files
     - Aggregate by hash (input generation combinations)
     - Process experiments (complete and incomplete)
     - Load git metadata, target info, LiteLLM stats
     - Corpus analysis with finder statistics
   - POV matching with three-tier strategy:
     - Tier 1: Sanitizer output matching
     - Tier 2: DEDUP_TOKEN matching
     - Tier 3: Error token fallback

3. **config.py** (~682 lines)
   - Centralized target configurations
   - Multiple configuration sets:
     - R2_TARGETS_CONFIG: Round 2 evaluation targets (7 targets)
     - R3_TARGETS_CONFIG: Round 3 evaluation targets (30+ targets)
     - OUR_TARGETS_CONFIG: Custom research targets (50+ targets)
   - Input generation combinations
   - Resource configuration (NCPU_PER_RUN, EVAL_DURATION_SECONDS)

4. **run_server.py** (~560 lines, estimated)
   - Web server for viewing experiment reports
   - Multi-date support with directory navigation
   - Authentication (customizable or disable)
   - HTTPS support with SSL certificates
   - Dynamic caching with visual indicators

5. **Supporting Modules**
   - **litellm_utils.py**: LiteLLM integration for AI-powered fuzzing
   - **utils.py**: CPU management, job queuing, system utilities
   - **generate_zips.py**: Result packaging and distribution
   - **verify_pov.py**: POV verification logic

## Key Design Patterns

### 1. Dynamic CPU Scheduling

```python
class CPUSlotManager:
    # Manages CPU core allocation
    # Automatic slot allocation and deallocation
    # Respects system CPU limits with warnings

class JobQueue:
    # Target collision prevention
    # Prevents multiple experiments on same target
    # Job tracking and status management
```

**Usage Pattern**:
```python
cpu_manager = CPUSlotManager(total_cores, NCPU_PER_RUN, start_core_idx)
job_queue = JobQueue(prevent_target_collision=True)

# Add jobs
job_info = JobInfo(target, hash_str, config_file, temp_multilang_root, cores_needed)
await job_queue.add_job(job_info.to_dict())

# Execute with allocated cores
await dispatch_jobs(cpu_manager, job_queue, execute_job, args, cleanup_event)
```

### 2. Temporary Workspace Isolation

**Strategy**: Copy-on-write isolated workspaces using temp directories
- Base codebase copied excluding benchmarks and oss-fuzz
- Only target project copied from benchmarks
- Artifacts and build outputs synced selectively
- Symlink libs/oss-fuzz/projects → benchmarks/projects

**Benefits**:
- Parallel execution without interference
- Clean separation of experiment data
- Artifacts synced back to main directory

### 3. Experiment Discovery

**Step-by-step approach** in `experiments.py`:
```python
def discover_all_experiments(eval_dir: Path):
    # Step 1: Fetch config files (from configs/*.json)
    configs = fetch_config_files(eval_dir)

    # Step 2: Aggregate by hash (input gen combinations)
    hash_groups = aggregate_by_hash(configs)

    # Step 3: Process ALL experiments (complete and incomplete)
    reports = process_all_experiments(eval_dir, hash_groups)

    # Step 4: Load git metadata once
    git_info = load_git_metadata(eval_dir)

    # Step 5: Enhance reports with target info, stats, corpus
    for report in reports:
        report.git_info = git_info
        report.target_info = load_target_info(report.target)
        report.experiment_stats = analyze_experiment_results(report, eval_dir)
        report.litellm_stats = load_litellm_metadata(eval_dir, ...)
        report.corpus_analysis = load_experiment_corpus_data(eval_dir, ...)
```

### 4. POV Matching Strategy

**Three-tier matching** for found POVs to expected CPVs:
```python
def match_povs_to_cpvs(found_povs, target_cpvs, harness_name, povs_dir):
    # Tier 1: Sanitizer output matching (highest priority)
    if found_pov.sanitizer_output in cpv.crash_log_content:
        found_pov.matched_cpv = cpv.name

    # Tier 2: DEDUP_TOKEN matching
    elif pov_dedup_token == cpv.dedup_token:
        found_pov.matched_cpv = cpv.name

    # Tier 3: Error token fallback
    elif cpv.error_token in crash_log_content:
        found_pov.matched_cpv = cpv.name
```

### 5. Resource Monitoring

**Multi-level monitoring**:
- **CPU Usage**: mpstat per-core tracking (5-minute intervals)
- **Docker Stats**: Container resource consumption
- **LiteLLM**: API usage, token counts, costs
- **Job Tracking**: Using CRS_JOB_ID environment variable

**Implementation**:
```python
# Start mpstat monitoring for allocated cores
core_list = ",".join(str(i) for i in range(start_core, end_core + 1))
mpstat_cmd = f"mpstat -P {core_list} 300 -o JSON > {resource_usage_file}"
subprocess.run(f"nohup {mpstat_cmd} 2>&1 &", shell=True, env=mpstat_env)

# Job runs with CRS_JOB_ID for tracking
env_dict["CRS_JOB_ID"] = job_info.job_id
subprocess.run(final_cmd, shell=True, env=env_dict)
```

## Directory Structure

```
eval_out/
├── configs/              # Generated configuration files
│   └── {target}/
│       └── {hash_str}.json
├── results/              # Experiment results
│   └── {hash_str}/
│       └── {target}/
│           ├── eval_result/        # Fuzzing results, PoVs, crash reports
│           │   ├── reports/        # HTML reports per harness
│           │   └── povs/          # Proof-of-vulnerability files
│           └── workdir_result/     # Working directories, intermediate files
├── stdout/               # Execution logs
│   └── {target}/
│       └── {hash_str}.txt
├── metadata/             # LiteLLM usage statistics
│   └── {target}/
│       └── {hash_str}.json
├── resource_usage/       # System resource monitoring
│   └── {target}/
│       └── {hash_str}.json
├── zipfiles/            # Generated ZIP packages
└── metadata.json        # Git repository metadata
```

**Hash String**: 16-character SHA256 hash of input generation combinations, used to uniquely identify experiment configurations.

## Data Models

### Experiment Tracking
- **ExperimentReport**: Complete experiment metadata
- **ExperimentStats**: POV discovery statistics
- **TargetInfo**: Target project information (language, repo, harnesses, CPVs)
- **FoundPoV**: Individual POV details
- **TargetCPV**: Expected vulnerability proof

### Git Metadata
- **GitInfo**: Main commit, date, submodules, dirty status
- **GitSubmodule**: Submodule path, commit, date

### Corpus Analysis
- **SeedInfo**: Individual seed metadata (hash, finder, corpus type)
- **FinderStats**: Per-finder statistics (POVs, corpus counts)
- **CorpusAnalysis**: Complete corpus analysis with finder stats

### LiteLLM
- **LiteLLMStats**: API usage statistics (spend, tokens, requests, caching)

## Execution Flow

### Phase 1: Config Setup
```bash
write_configs(out_dir, target, harnesses_list, cores_per_cp)
# Creates JSON configs for all target/input-gen combinations
```

### Phase 2: Metadata Collection
```bash
collect_experiment_metadata(multilang_root)
# Captures git repository state for reproducibility
# Saves to eval_dir/metadata.json
```

### Phase 3: Status Analysis
```bash
analyze_experiments_status(out_dir, experiment_info)
# Checks for completed/incomplete experiments
# Cleans up incomplete experiments
```

### Phase 4: Image Building
```bash
build_crs(multilang_root)  # Base image
build_cp(multilang_root, target)  # Target images
```

### Phase 5: Dynamic Job Scheduling
```bash
# Create CPU manager and job queue
cpu_manager = CPUSlotManager(...)
job_queue = JobQueue(prevent_target_collision=True)

# Add jobs with resource requirements
for target, configs in experiment_info.items():
    job_info = JobInfo(target, hash_str, config_file, ...)
    await job_queue.add_job(job_info.to_dict())

# Execute with monitoring
await dispatch_jobs(cpu_manager, job_queue, execute_job, ...)
```

### Phase 6: ZIP Generation
```bash
generate_aggregate_zips(out_dir, multilang_root)
# Package results for distribution
```

## Command Line Usage

### Run Evaluation
```bash
# Basic usage
python run_eval.py --out-dir ~/eval_output

# Comprehensive example with all options
python run_eval.py \
  --out-dir ~/eval_output/2025-06-24-r3-all \
  --multilang-root ~/CRS-multilang \
  --copy-workdir \
  --start-other-services \
  --cores-per-cp \
  --skip-existing-images

# Options:
#   --multilang-root PATH     Path to CRS-multilang directory
#   --out-dir PATH           Output directory
#   --start-core-idx INT     Starting core index (default: 0)
#   --cores-per-cp           Use NCPU_PER_RUN cores per CP instead of per harness
#   --start-other-services   Start additional services during evaluation
#   --copy-workdir           Copy working directories to results
#   --dont-cleanup-temps     Don't clean up temporary directories
#   --skip-existing-images   Skip building if Docker images already exist
```

### Run Web Server
```bash
# Basic server
python run_server.py

# Custom configuration
python run_server.py \
  --root-eval-dir ~/eval_output \
  --port 12345 \
  --cache-duration 1200 \
  --multilang-root ~/CRS-multilang

# HTTPS with SSL
python run_server.py \
  --cert-path ./keys/fullchain.pem \
  --key-path ./keys/privkey.pem

# Options:
#   --root-eval-dir PATH      Root directory containing dated evaluation results
#   --multilang-root PATH     Path to CRS-multilang root directory
#   --default-date DATE       Default date to display (YYYY-MM-DD or 'latest')
#   --port INT               Port to serve on (default: 43434)
#   --username USER          Username for basic auth (default: admin)
#   --password PASS          Password for basic auth (default: atlantis1!)
#   --no-auth                Disable authentication
#   --cache-duration INT     Cache duration in seconds (default: 300)
```

## Key Learnings for CRSBench

### 1. Dynamic CPU Scheduling
**Relevance**: High - CRSBench needs similar resource management
- Slot-based allocation with automatic core assignment
- Target collision prevention for parallel execution
- Graceful cleanup with signal handling

**Implementation Ideas**:
- Adopt CPUSlotManager pattern for worker allocation
- Use JobQueue with target collision prevention
- Track jobs using unique identifiers (CRS_JOB_ID pattern)

### 2. Temporary Workspace Isolation
**Relevance**: Medium - CRSBench may need isolated workspaces
- Copy-on-write strategy for parallel execution
- Selective syncing of artifacts and build outputs
- Symlink strategy for shared resources

**Implementation Ideas**:
- Consider temp workspaces for parallel trials
- Sync results back to main directory after completion
- Use rsync with exclude patterns for efficient copying

### 3. Experiment Discovery and Analysis
**Relevance**: High - CRSBench needs similar reporting
- Step-by-step discovery from config files
- Aggregate by hash (input generation combinations)
- Track complete vs incomplete experiments
- Load metadata, stats, and corpus analysis

**Implementation Ideas**:
- Implement discover_experiments() pattern
- Use hash-based organization for configurations
- Track experiment status (not started, started, complete)

### 4. POV Matching Strategy
**Relevance**: High - CRSBench needs POV verification
- Three-tier matching: sanitizer output → DEDUP_TOKEN → error token
- Distinguish between matched (intended) and unintended POVs
- Server verification for additional validation

**Implementation Ideas**:
- Adopt three-tier matching strategy
- Use DEDUP_TOKEN for crash deduplication
- Track both intended and unintended discoveries

### 5. Git Metadata Tracking
**Relevance**: High - CRSBench needs reproducibility
- Capture main commit, date, and all submodules
- Track dirty status and uncommitted changes
- Store metadata.json for each experiment

**Implementation Ideas**:
- Implement collect_experiment_metadata() pattern
- Store git info at experiment start
- Include submodule tracking for reproducibility

### 6. Resource Monitoring
**Relevance**: Medium - CRSBench may need performance tracking
- mpstat for CPU usage per core
- Docker stats for container resources
- LiteLLM for API usage and costs

**Implementation Ideas**:
- Add optional resource monitoring
- Track CPU usage per trial
- Monitor LiteLLM costs if using AI-powered CRS

### 7. Web Interface for Reporting
**Relevance**: Medium - CRSBench could benefit from web UI
- Multi-date support with directory navigation
- Authentication and HTTPS support
- Dynamic caching with visual indicators

**Implementation Ideas**:
- Consider web interface for Phase 5 (Documentation)
- Use Flask/FastAPI for lightweight server
- Implement date-based navigation for experiments

## Configuration Examples

### Target Configuration
```python
R3_TARGETS_FILTERED_CONFIG2 = {
    "aixcc/c/r3-curl-delta-01": ["curl_fuzzer_ws"],
    "aixcc/jvm/r3-apache-commons-compress": ["CompressTarFuzzer"],
    "aixcc/jvm/r3-tika-delta-03": ["ThreeDXMLParserFuzzer"],
    "aixcc/jvm/r3-tika": ["TikaAppUnpackerFuzzer"],
    "aixcc/jvm/r3-zookeeper": ["MultiProcessTxnFuzzer"],
}
```

### Input Generation Combinations
```python
INPUT_GEN_COMBINATIONS = [
    ["given_fuzzer", "concolic_input_gen", "testlang_input_gen", "dict_input_gen", "mlla"],
]
```

### Resource Configuration
```python
NCPU_PER_RUN = 24                    # CPU cores per harness
EVAL_DURATION_SECONDS = 60 * 60 * 2  # 2 hours per experiment
```

## Testing

Located in `tests/` directory:
- `test_cpu_manager.py`: CPU slot management tests
- `test_signal_handling.py`: Signal handling tests
- `test_litellm_utils.py`: LiteLLM integration tests
- `test_multiple_cycles.py`: Multi-cycle experiment tests
- `test_target_collision.py`: Target collision prevention tests

## Dependencies

### System Requirements
- Linux system with Docker support
- Python 3.8+
- 32+ CPU cores, 16GB+ RAM, 100GB+ disk recommended

### Python Dependencies
- `pyenv` for environment management
- `dotenv` for configuration
- `loguru` for logging
- `yaml` for config parsing
- `asyncio` for async operations

### External Tools
- `tmux` for session management
- `rsync` for file syncing
- `mpstat` for CPU monitoring
- Docker for containerization

## Notes

- **OSS-Fuzz Integration**: Uses OSS-Fuzz compatible project structures
- **Delta/Diff Mode**: Supports delta variants for testing specific changes
- **LiteLLM Required**: Create `.env.secret` with LITELLM_MASTER_KEY and LITELLM_URL
- **Hash-based Organization**: 16-character SHA256 hash identifies configurations
- **Graceful Cleanup**: Signal handlers for clean experiment termination

## Related Documentation

- [FuzzBench Redis Architecture](./fuzzbench-redis-architecture.md) - Distributed fuzzing with Redis
- Main README: `claude_reference_projects/crs-multilang-e2e-eval/README.md`
