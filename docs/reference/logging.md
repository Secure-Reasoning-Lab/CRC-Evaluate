# CRSBench Logger Usage Guide

## Quick Start

### Import and Use

```python
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

logger.info("Application started")
logger.warning("Configuration file not found, using defaults")
logger.error("Failed to connect to database")
```

### Output Format

```
2025-11-20 21:17:02 | INFO     | [distributed/worker]            | Application started
2025-11-20 21:17:03 | WARNING  | [distributed/worker]            | Configuration file not found
2025-11-20 21:17:04 | ERROR    | [distributed/worker]            | Failed to connect to database
```

## Log Levels

### Available Levels (from lowest to highest)

1. **TRACE** - Very detailed debugging (rarely used)
2. **DEBUG** - Detailed debugging information
3. **INFO** - General informational messages (default)
4. **SUCCESS** - Success confirmations (loguru-specific)
5. **WARNING** - Warning messages
6. **ERROR** - Error messages
7. **CRITICAL** - Critical failures

### Usage Examples

```python
logger.trace("Entering function with args: x=1, y=2")
logger.debug(f"Processing {len(items)} items")
logger.info("Trial execution started")
logger.success("All tests passed!")
logger.warning("Deprecated configuration detected")
logger.error("Failed to read file: config.yaml")
logger.critical("System out of memory")
```

## Configuration

### CLI Verbose Mode

Set DEBUG logs explicitly per command:

```bash
# Show all messages including DEBUG
crsbench worker --experiment-config config.yaml --verbose

# Evaluator verbose mode
crsbench evaluator --experiment-config config.yaml --verbose
```

### Programmatic Configuration

```python
from crsbench.utils.logger import configure_logger

# Change log level
configure_logger(level="DEBUG")

# Disable colors (useful for file output)
configure_logger(level="INFO", colorize=False)

# Change output destination
import sys
configure_logger(sink=sys.stderr, level="ERROR")
```

## Module Path Display

The logger automatically formats module names hierarchically:

| Original Module Name          | Displayed As                |
|-------------------------------|-----------------------------|
| `crsbench.distributed.worker` | `[distributed/worker]`      |
| `crsbench.evaluation.runner`  | `[evaluation/runner]`       |
| `crsbench.migration.repo_manager` | `[migration/repo_manager]` |
| `crsbench.run_experiment`     | `[run_experiment]`          |

This makes it easy to identify which component is generating each log message.

## Color Scheme

When outputting to a terminal (TTY), logs are automatically colored:

- **TRACE**: Dim Cyan
- **DEBUG**: Blue
- **INFO**: White
- **SUCCESS**: Green
- **WARNING**: Yellow
- **ERROR**: Red
- **CRITICAL**: Bold Red

Colors are automatically disabled when:
- Output is redirected to a file
- Running in non-interactive environments
- `colorize=False` is explicitly set

## Advanced Usage

### Exception Logging

```python
try:
    risky_operation()
except Exception:
    logger.exception("Operation failed")
    # This automatically includes traceback
```

For non-error levels where you still want traceback context:

```python
try:
    refresh_cache()
except Exception:
    logger.opt(exception=True).warning("Cache refresh failed")
```

### Structured Logging

```python
logger.info("User login", user_id=123, ip="192.168.1.1")
```

### Module-Level Quick Functions

For quick logging without creating a logger instance:

```python
from crsbench.utils import info, warning, error, debug, success

info("Quick info message")
warning("Quick warning")
error("Quick error")
debug("Quick debug message")
success("Operation completed")
```

### Backwards Compatibility

For code still using standard `logging` module patterns:

```python
from crsbench.utils.logger import getLogger

# Drop-in replacement for logging.getLogger()
logger = getLogger(__name__)
logger.info("This works just like logging module")
```

## Best Practices

### 1. Always Use `__name__`

```python
# ✓ Good
logger = get_logger(__name__)

# ✗ Bad
logger = get_logger("my_module")
```

### 2. Use Appropriate Log Levels

```python
# ✓ Good
logger.debug("Detailed processing info")    # For developers
logger.info("Trial started")                # For users
logger.warning("Deprecated feature used")   # For attention
logger.error("Operation failed")            # For errors

# ✗ Bad
logger.info("x=1, y=2, z=3...")            # Use debug instead
logger.error("Trial completed")             # Use info/success instead
```

### 3. Use Loguru Formatting

With `get_logger()`, do not use stdlib logging placeholders like `%s` or flags
like `exc_info=True`. CRSBench uses Loguru formatting and exception APIs.

```python
# ✓ Good
logger.info("Processing {} files", count)
logger.info(f"Processing {count} files")
logger.exception("Coverage collection failed")
logger.opt(exception=True).warning("Cache refresh failed")

# ✗ Bad (stdlib logging patterns)
logger.info("Processing %s files", count)
logger.error("Coverage collection failed", exc_info=True)
```

### 4. Don't Log Sensitive Data

```python
# ✗ Bad
logger.info(f"API key: {api_key}")
logger.debug(f"Password: {password}")

# ✓ Good
logger.info("API authentication successful")
logger.debug("User authenticated")
```

### 5. Use SUCCESS for Positive Outcomes

```python
# ✓ Good
logger.success("All POVs found successfully")
logger.success("Build completed without errors")

# ✗ Bad (less clear)
logger.info("All POVs found successfully")
```

## Troubleshooting

### Logs Not Appearing

Check log level:
```python
from crsbench.utils.logger import configure_logger
configure_logger(level="DEBUG")  # See all messages
```

### No Colors in Output

Colors only work in TTY:
```bash
# ✓ Colors enabled
crsbench run --experiment-config config.yaml

# ✗ Colors disabled (redirected)
crsbench run --experiment-config config.yaml > output.log
```

Force colors:
```python
from crsbench.utils.logger import configure_logger
configure_logger(colorize=True)
```

### Module Path Not Showing

Ensure you're using `get_logger(__name__)`:
```python
from crsbench.utils.logger import get_logger
logger = get_logger(__name__)  # __name__ is required
```

## Examples from CRSBench

### Distributed Worker
```python
# crsbench/distributed/worker.py
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

logger.info("Worker started")
logger.debug(f"Queue status: {queue.count} jobs")
logger.success("Connected to Redis successfully")
```

Output:
```
2025-11-20 10:15:32 | INFO     | [distributed/worker]            | Worker started
2025-11-20 10:15:32 | DEBUG    | [distributed/worker]            | Queue status: 5 jobs
2025-11-20 10:15:33 | SUCCESS  | [distributed/worker]            | Connected to Redis successfully
```

### Evaluation Runner
```python
# crsbench/evaluation/runner.py
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

logger.info("Starting CRS trial execution")
logger.warning("Docker build took longer than expected")
logger.success("Trial completed successfully")
```

Output:
```
2025-11-20 10:16:45 | INFO     | [evaluation/runner]             | Starting CRS trial execution
2025-11-20 10:18:12 | WARNING  | [evaluation/runner]             | Docker build took longer than expected
2025-11-20 10:20:34 | SUCCESS  | [evaluation/runner]             | Trial completed successfully
```

## Migration from Standard Logging

### Before
```python
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

logger.info("Message")
```

### After
```python
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

logger.info("Message")
```

Much simpler and with better default formatting!
