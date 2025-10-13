"""Worker implementation for CRSBench distributed execution.

This module implements the worker process that connects to the Redis queue,
pulls jobs, executes them, and reports results back to the queue.

Workers can be run locally or deployed in containers for horizontal scaling.
"""

import os
import time
import logging
import sys

try:
    import redis
    import rq
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

logger = logging.getLogger(__name__)


def main():
    """
    Worker entry point - connects to Redis and processes jobs.

    Environment Variables:
        REDIS_HOST: Redis server hostname (default: localhost)
        EXPERIMENT_NAME: Experiment identifier for queue naming (default: default)
        WORKER_TIMEOUT: Job timeout in seconds (default: 3600)
        LOG_LEVEL: Logging level (default: INFO)

    Usage:
        # Run as module
        python -m crsbench.distributed.worker

        # Run in Docker
        docker run -e REDIS_HOST=redis-server -e EXPERIMENT_NAME=exp1 crsbench-worker

    Exit Codes:
        0: Normal shutdown (queue empty)
        1: Redis/RQ not installed
        2: Cannot connect to Redis
        3: Worker error
    """
    # Configure logging
    log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
    logging.basicConfig(
        level=getattr(logging, log_level),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        stream=sys.stdout
    )

    # Check if Redis/RQ available
    if not REDIS_AVAILABLE:
        logger.error("Redis and RQ packages are required for worker execution")
        logger.error("Install with: pip install redis rq")
        sys.exit(1)

    # Get configuration from environment
    redis_host = os.environ.get('REDIS_HOST', 'localhost')
    experiment_name = os.environ.get('EXPERIMENT_NAME', 'default')
    worker_timeout = int(os.environ.get('WORKER_TIMEOUT', '3600'))

    logger.info("="*60)
    logger.info("CRSBench Distributed Worker")
    logger.info("="*60)
    logger.info(f"Redis host: {redis_host}")
    logger.info(f"Experiment: {experiment_name}")
    logger.info(f"Worker timeout: {worker_timeout}s")
    logger.info("="*60)

    try:
        # Connect to Redis
        logger.info(f"Connecting to Redis at {redis_host}...")
        redis_connection = redis.Redis(host=redis_host, socket_connect_timeout=5)

        # Test connection
        redis_connection.ping()
        logger.info("✓ Connected to Redis successfully")

        # Set up RQ connection context
        with rq.Connection(redis_connection):
            queue_name = f'crsbench_{experiment_name}'
            queue = rq.Queue(queue_name)
            worker = rq.Worker([queue])

            logger.info(f"Worker started, listening on queue: {queue_name}")
            logger.info("Waiting for jobs...")

            # Work in burst mode until queue is empty
            # Burst mode: process available jobs then exit (vs. continuous polling)
            while queue.count + queue.deferred_job_registry.count > 0:
                logger.debug(
                    f"Queue status: {queue.count} queued, "
                    f"{queue.deferred_job_registry.count} deferred"
                )

                # Process jobs with timeout
                worker.work(
                    burst=True,  # Exit after processing available jobs
                    max_jobs=1,  # Process one job at a time for better logging
                )

                # Brief sleep to allow queue state to update
                time.sleep(2)

            logger.info("="*60)
            logger.info("Queue empty, worker shutting down")
            logger.info("="*60)

    except redis.ConnectionError as e:
        logger.error(f"Cannot connect to Redis at {redis_host}: {e}")
        logger.error("Please check that Redis server is running and accessible")
        sys.exit(2)

    except redis.TimeoutError as e:
        logger.error(f"Connection to Redis timed out: {e}")
        sys.exit(2)

    except KeyboardInterrupt:
        logger.info("\nReceived interrupt signal, shutting down gracefully...")
        sys.exit(0)

    except Exception as e:
        logger.error(f"Worker error: {e}", exc_info=True)
        sys.exit(3)


def run_worker_continuous(
    redis_host: str,
    experiment_name: str,
    timeout: int = 3600
):
    """
    Run worker in continuous mode (polling indefinitely).

    Unlike the main() function which runs in burst mode, this function
    runs continuously and never exits (except on error or interrupt).

    Args:
        redis_host: Redis server hostname
        experiment_name: Experiment identifier for queue naming
        timeout: Job execution timeout in seconds

    Note:
        This mode is useful for long-running worker deployments where
        the worker should continuously process jobs as they arrive.
    """
    if not REDIS_AVAILABLE:
        raise RuntimeError("Redis and RQ packages are required")

    logger.info(f"Starting continuous worker for experiment: {experiment_name}")

    try:
        redis_connection = redis.Redis(host=redis_host)
        redis_connection.ping()

        with rq.Connection(redis_connection):
            queue_name = f'crsbench_{experiment_name}'
            queue = rq.Queue(queue_name)
            worker = rq.Worker([queue])

            logger.info(f"Worker running in continuous mode on queue: {queue_name}")

            # Run worker in continuous mode (never exits)
            worker.work(
                burst=False,  # Continuous mode
                logging_level=logging.INFO
            )

    except KeyboardInterrupt:
        logger.info("Received interrupt, shutting down...")
    except Exception as e:
        logger.error(f"Worker error: {e}", exc_info=True)
        raise


if __name__ == '__main__':
    main()
