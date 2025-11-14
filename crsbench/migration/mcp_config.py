"""Configuration for CRSBench MCP server."""

import os

# CRSBench repository root (auto-detected from this file's location)
CRSBENCH_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))

# Base directory for MCP operations (can be overridden by env var)
# Default to .crsbench-mcp under the working directory instead of home directory
BASE_DIR = os.getenv('CRSBENCH_MCP_DIR', os.path.join(CRSBENCH_ROOT, '.crsbench-mcp'))

# Directory for project source code clones
# Use PROJECT_REPOS_DIR if set, otherwise use ~/.crsbench-mcp/projects
BASE_PROJECTS_DIR = os.getenv('PROJECT_REPOS_DIR', f'{BASE_DIR}/projects')

# CRSBench benchmarks directory (use actual benchmarks/ in the repo)
BASE_BENCHMARKS_DIR = os.path.join(CRSBENCH_ROOT, 'benchmarks')

# Temporary logs directory
BASE_TMP_LOGS = f'{BASE_DIR}/tmp-logs'

# OSS-Fuzz directory (submodule in CRSBench repo)
OSS_FUZZ_DIR = os.path.join(CRSBENCH_ROOT, 'oss-fuzz')

# OSS-Fuzz projects directory where benchmarks will be copied
# Benchmarks will be under oss-fuzz/projects/aixcc/
OSS_FUZZ_PROJECTS_DIR = os.path.join(OSS_FUZZ_DIR, 'projects')
OSS_FUZZ_AIXCC_DIR = os.path.join(OSS_FUZZ_PROJECTS_DIR, 'aixcc')

# Docker image prefix for CRSBench
DOCKER_IMAGE_PREFIX = 'crsbench'
