# PatchAgent

## Overview

PatchAgent is an LLM-based program repair agent that automatically generates patches for real-world vulnerabilities by mimicking human expertise. Presented at USENIX Security 2025, it integrates Language Server Protocol for code analysis, patch verification, and interaction optimization to achieve human-like reasoning during vulnerability repair.

## Key Features

- **Multi-language Support**: C/C++ and Java
- **Multiple Sanitizer Support**: ASan, MSan, UBSan, TSan, Jazzer
- **OSS-Fuzz Integration**: Compatible with Google OSS-Fuzz projects
- **Language Server Protocol**: Accurate code navigation and analysis
- **Patch Verification**: Automated validation of generated fixes
- **Real-world Impact**: Successfully fixed vulnerabilities in popular open-source projects

## Architecture

### Core Components

1. **PatchTask** (`patchagent/task.py`): Main orchestrator that manages the repair workflow
   - Initializes repair environment
   - Validates patches through build/test cycles
   - Manages proof-of-concept (PoC) files
   - Tracks repair context and history

2. **BaseAgent** (`patchagent/agent/base.py`): Abstract base class for repair agents
   - Retry mechanism for API failures
   - Exception handling for validation errors
   - Standardized agent interface

3. **Builder System** (`patchagent/builder/`): Handles project building and testing
   - Abstract Builder class with common functionality
   - OSS-Fuzz specific implementation
   - Git-based patch management
   - Language-specific build processes

4. **Parser System** (`patchagent/parser/`): Sanitizer report analysis
   - Support for multiple sanitizer types (ASan, MSan, UBSan, TSan, Jazzer)
   - Structured report parsing
   - Error categorization and analysis

5. **Language Server Protocol** (`patchagent/lsp/`): Code analysis and navigation
   - Clangd integration for C/C++
   - Java language server support
   - Symbol resolution and code understanding

### Agent Generation

The agent generator (`patchagent/agent/generator.py`) creates repair agents with configurable parameters:
- **Model selection**: Supports different LLM models (default: GPT-4o)
- **Temperature variations**: 0, 0.3, 0.7, 1.0 for different creativity levels
- **Auto-hint mechanisms**: Enables/disables automatic code hints
- **Counterexample handling**: Configurable number of counterexamples
- **Fast mode**: Single configuration for quick repairs

## Workflow

### 1. Initialization
```python
patchtask = PatchTask(
    [OSSFuzzPoC("poc.bin", "target_name")],
    OSSFuzzBuilder("project", source_path, ossfuzz_path, sanitizers)
)
patchtask.initialize()
```

### 2. Repair Process
```python
patch = patchtask.repair(agent_generator())
```

### 3. Validation Pipeline
- **Patch Format Check**: Validates Git patch syntax
- **Build Verification**: Ensures code compiles successfully
- **PoC Replay**: Tests if bug is fixed
- **Function Tests**: Runs existing test suites

## Integration with CRSBench

PatchAgent serves as a reference implementation for:

1. **Agent-based Repair**: Demonstrates LLM-driven vulnerability fixing
2. **Multi-sanitizer Support**: Shows handling of different bug detection tools
3. **Validation Framework**: Provides comprehensive patch testing methodology
4. **Language Agnostic Design**: Architecture supports multiple programming languages

## Technical Specifications

- **Python Version**: 3.12+
- **Key Dependencies**:
  - `anthropic>=0.55.0` - LLM API integration
  - `langchain>=0.3.19` - LLM workflow management
  - `tree-sitter>=0.24.0` - Code parsing
  - `GitPython>=3.1.44` - Git operations
  - `clang==16.0.1` - C/C++ analysis

## Real-world Impact

PatchAgent has successfully fixed vulnerabilities in notable open-source projects:
- **assimp** (11.4k stars): 3 vulnerability fixes
- **libssh2** (1.4k stars): 1 vulnerability fix
- **hdf5** (0.6k stars): 2 vulnerability fixes
- **libredwg** (1.0k stars): 1 vulnerability fix
- **Pcap++** (2.8k stars): 3 vulnerability fixes

## Relevance to CRSBench

PatchAgent provides valuable insights for CRSBench development:

1. **Benchmark Design**: Shows how to structure vulnerability repair tasks
2. **Evaluation Metrics**: Demonstrates comprehensive validation approaches
3. **Tool Integration**: Illustrates effective use of sanitizers and language servers
4. **Agent Architecture**: Provides patterns for LLM-based CRS implementation
5. **Real-world Validation**: Offers proven methodology for testing repairs

## Configuration

The system uses environment variables for API keys and configuration, supporting:
- Multiple LLM providers through LangChain
- Configurable timeout values
- Custom workspace management
- Sanitizer-specific settings

## License

Apache License 2.0