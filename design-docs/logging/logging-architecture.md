# Logging Architecture

## Overview

CRSBench uses a centralized logging system based on [loguru](https://loguru.readthedocs.io/) for consistent, colored, and hierarchical log output across all modules.

## Design Principles

1. **Single Source of Truth**: All logging goes through `crsbench/utils/logger.py`
2. **Module Hierarchy Display**: Logs show clear module paths (e.g., `[distributed/worker]`, `[evaluation/runner]`)
3. **Automatic Color Management**: Colors enabled for TTY, disabled for file redirection
4. **Environment-Based Configuration**: Log level controlled via `LOG_LEVEL` environment variable
5. **Backwards Compatibility**: Provides adapter for standard `logging` module patterns

## Architecture

```
┌─────────────────────────────────────────┐
│   crsbench/utils/logger.py              │
│   (Centralized Logger)                  │
│                                         │
│   - loguru wrapper                      │
│   - TTY detection                       │
│   - Module path formatting              │
│   - Color scheme configuration          │
└───────────────┬─────────────────────────┘
                │
                │ imported by
                │
    ┌───────────┴──────────────┐
    │                           │
    ▼                           ▼
┌─────────┐            ┌──────────────┐
│ Core    │            │ Modules      │
│ Modules │            │              │
├─────────┤            ├──────────────┤
│ • run_  │            │ • distributed│
│   exp   │            │ • evaluation │
│         │            │ • migration  │
│         │            │ • benchmark_ │
│         │            │   ci         │
└─────────┘            └──────────────┘
```

## Implementation

### Logger Module (`crsbench/utils/logger.py`)

**Core Components:**

1. **Logger Instance**: Singleton loguru logger with custom configuration
2. **Format Function**: `_format_module_path()` converts module names to hierarchical paths
3. **Custom Formatter**: `_custom_formatter()` applies color scheme based on log level
4. **Configuration Function**: `configure_logger()` for runtime reconfiguration

**Features:**

- Automatic TTY detection
- Colored output with level-specific color schemes
- Module path formatting: `crsbench.distributed.worker` → `[distributed/worker]`
- Support for all log levels: TRACE, DEBUG, INFO, SUCCESS, WARNING, ERROR, CRITICAL
- Environment variable configuration (`LOG_LEVEL`)

### Log Format

**Terminal Output (TTY):**
```
YYYY-MM-DD HH:mm:ss | LEVEL    | [module/path]                   | message
2025-11-21 11:24:23 | INFO     | [distributed/worker]            | Worker started
2025-11-21 11:24:24 | ERROR    | [evaluation/runner]             | Trial failed
2025-11-21 11:24:25 | SUCCESS  | [migration/repo_manager]        | Sync complete
```

**File Output (non-TTY):**
```
YYYY-MM-DD HH:mm:ss | LEVEL    | [module/path]                   | message
(Same format but without ANSI color codes)
```

### Color Scheme

| Level    | Color          | Terminal Display |
|----------|----------------|------------------|
| TRACE    | Dim Cyan       | Very detailed debugging |
| DEBUG    | Blue           | Debugging information |
| INFO     | White          | General information |
| SUCCESS  | Green          | Success confirmations |
| WARNING  | Yellow         | Warnings |
| ERROR    | Red            | Errors |
| CRITICAL | Bold Red       | Critical failures |

## Usage Patterns

### Standard Usage

```python
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

logger.debug("Detailed debugging info")
logger.info("General information")
logger.success("Operation succeeded")
logger.warning("Warning message")
logger.error("Error occurred")
logger.critical("Critical failure")
```

### Module-Level Quick Logging

```python
from crsbench.utils import info, warning, error

info("Quick info message")
warning("Quick warning")
error("Quick error")
```

### Configuration

```python
from crsbench.utils.logger import configure_logger

# Change log level
configure_logger(level="DEBUG")

# Disable colors
configure_logger(colorize=False)

# Change output sink
import sys
configure_logger(sink=sys.stderr, level="WARNING")
```

### Environment Variable

```bash
# Set log level globally
export LOG_LEVEL=DEBUG
crsbench run --experiment-config config.yaml ...
```

## Migration from Standard Logging

### Before (Standard Logging)

```python
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

logger.info("Message")
```

### After (Loguru via CRSBench)

```python
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

logger.info("Message")
```

## Benefits

1. **Consistency**: All modules use the same logging system
2. **Visibility**: Clear module hierarchy in logs makes debugging easier
3. **Automatic Color Management**: Works correctly in all environments
4. **Simpler API**: No need for `basicConfig()` or manual setup
5. **Better Defaults**: Sensible formatting out of the box
6. **Environment Control**: Easy runtime configuration via `LOG_LEVEL`

## Module Coverage

### Fully Converted Modules

All modules in CRSBench use the centralized logger:

- **Core**: `run_experiment.py`
- **Distributed**: All 3 modules
- **Evaluation**: All 9 modules
- **Migration**: All 6 modules
- **Validation**: `format_validator.py`
- **Hint Generation**: `generate_hints.py`
- **Benchmark CI**: All 5 modules

### Legacy Logging

The old `benchmark_ci/logger.py` module is no longer used. It has been superseded by `crsbench/utils/logger.py`.

## Testing

Comprehensive test suite at `tests/test_logger.py` covers:

- Logger creation and configuration
- All log levels
- TTY detection
- Color management
- Level filtering
- Backwards compatibility
- Environment variable support

Run tests:
```bash
uv run pytest tests/test_logger.py -v
```

## Performance Considerations

- Loguru is lazy-evaluated, so there's minimal performance overhead
- String formatting only occurs when messages are actually logged
- TTY detection is cached at module import time
- No file I/O unless explicitly configured

## Future Enhancements

1. **Structured Logging**: Add JSON output mode for production
2. **Log Rotation**: Add file rotation support for long-running processes
3. **Context Binding**: Bind trial/benchmark information to logger context
4. **Remote Logging**: Support for centralized log aggregation
5. **Performance Metrics**: Add timing/profiling decorators

## References

- Implementation: `crsbench/utils/logger.py`
- Tests: `tests/test_logger.py`
- Usage Guide: `docs/logger-usage-guide.md`
- Migration Summary: `docs/logging-migration-summary.md`
- Loguru Documentation: https://loguru.readthedocs.io/
