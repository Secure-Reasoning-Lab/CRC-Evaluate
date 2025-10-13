"""Distributed job queue module for CRSBench.

This module provides distributed execution capabilities using Redis and RQ (Redis Queue).
It enables horizontal scaling of CRS trial execution across multiple worker processes.

Main Components:
    - jobs: Job definitions for CRS trial execution
    - worker: Worker implementation for processing jobs from queue
    - queue: Queue utilities for Redis/RQ management

Usage:
    # Distributed execution (orchestrator)
    from crsbench.distributed.queue import initialize_queue
    from crsbench.distributed.jobs import run_crs_trial

    queue = initialize_queue('localhost', 'experiment-name')
    job = queue.enqueue(run_crs_trial, crs='test-crs', benchmark='test-bench', ...)

    # Worker execution
    from crsbench.distributed.worker import main as worker_main
    worker_main()
"""

from crsbench.distributed.jobs import (
    build_crs_environment,
    run_crs_trial,
    evaluate_crs_trial,
)

from crsbench.distributed.queue import (
    initialize_queue,
    get_all_jobs,
    check_redis_available,
)

__all__ = [
    'build_crs_environment',
    'run_crs_trial',
    'evaluate_crs_trial',
    'initialize_queue',
    'get_all_jobs',
    'check_redis_available',
]
