# CRSBench Migration Module

## Overview

The migration module provides an intelligent, LangGraph-powered system for automatically converting benchmark configurations from legacy formats to the unified CRSBench YAML specification. This module uses multiple specialized agents to handle the complex task of format migration while preserving all vulnerability data and configuration details.

## Purpose

This module addresses the need to migrate from multiple existing benchmark formats to a single, standardized format:

- **Internal format**: Used during CRS development (config.yaml with cpvs terminology)
- **Official AIxCC format**: Competition benchmarks (challenge.yaml with external vulnerability references)
- **New unified format**: Standardized meta.yaml with povs terminology and comprehensive features

## Architecture

### LangGraph Multi-Agent System

The migration system employs a graph-based workflow with specialized AI agents:

```
Input Detection → Format Analysis → Data Extraction → Format Conversion → Validation → Output Generation
```

### Core Agents

#### 1. **Format Detection Agent**
- **Purpose**: Automatically identify source benchmark format
- **Capabilities**:
  - Detect internal vs. official AIxCC format
  - Identify configuration file structure
  - Determine migration complexity level
  - Handle edge cases and format variations

#### 2. **Analysis Agent**
- **Purpose**: Deep analysis of existing benchmark structure
- **Capabilities**:
  - Parse YAML configurations comprehensively
  - Extract vulnerability definitions (CPVs/POVs)
  - Identify harness specifications
  - Map file relationships and dependencies
  - Preserve metadata and comments

#### 3. **Conversion Agent**
- **Purpose**: Transform data to new unified format
- **Capabilities**:
  - Convert cpvs terminology to povs
  - Merge inline and external vulnerability definitions
  - Generate comprehensive patch_exclude_list
  - Normalize harness configurations
  - Preserve all functional semantics

#### 4. **Validation Agent**
- **Purpose**: Ensure migration completeness and correctness
- **Capabilities**:
  - Verify all data preservation
  - Check YAML schema compliance
  - Validate POV configurations
  - Ensure file structure integrity
  - Generate migration reports

## Migration Workflow

### Stage 1: Discovery and Analysis
- Scan directory structure for benchmark files
- Identify configuration files (config.yaml, challenge.yaml, etc.)
- Analyze vulnerability definitions and harness specifications
- Build dependency map of files and relationships

### Stage 2: Format Mapping
- Map old format fields to new unified schema
- Convert terminology (cpvs → povs)
- Merge external vulnerability references into main config
- Normalize path specifications and references

### Stage 3: Data Transformation
- Generate new meta.yaml configuration
- Consolidate harness and POV specifications
- Create comprehensive patch exclusion lists
- Preserve all evaluation modes (delta/full)

### Stage 4: Validation and Output
- Validate generated configuration against schema
- Check completeness of migration
- Generate migration summary report
- Output new benchmark structure

## Input Formats

### Internal Format (config.yaml)
```yaml
full_mode:
  base_commit: "..."
delta_mode:
  - base_commit: "..."
    ref_commit: "..."
harness_files:
  - name: "fuzz_nm"
    path: "$PROJECT/fuzz_nm.c"
    cpvs:  # Legacy terminology
      - name: "cpv_1"
        sanitizer: "address"
        error_token: "AddressSanitizer: heap-use-after-free"
```

### Official AIxCC Format (challenge.yaml)
```yaml
metadata_spec_version: v1
name: 'Challenge Name'
challenge_type: full
vulnerabilities:
  - systemd-001
  - systemd-003
harnesses:
  - 'fuzz-bus-match'
  - 'fuzz-catalog'
patch_exclude_list:
  - 'test/*'
  - 'src/test/*'
```

## Output Format

### Unified meta.yaml
```yaml
# Comprehensive patch exclusion list
patch_exclude_list:
  - "build.sh"
  - "test/**"
  - "**/*test*.c"

# Evaluation modes
delta_mode:
  base_commit: "..."
  ref_commit: "..."

full_mode:
  base_commit: "..."

# Harness specifications with embedded POVs
harness_files:
  - name: "fuzz_nm"
    path: "$REPO/test/fuzz_nm.c"
    povs:  # Standardized terminology
      - name: "pov_1"
        sanitizer: "address"
        error_token: "AddressSanitizer: heap-use-after-free"
        requires_clean_build: false
```

## Features

### Intelligent Migration
- **Context-aware conversion**: Understands semantic meaning of configurations
- **Data preservation**: Ensures no information loss during migration
- **Error handling**: Graceful handling of malformed or incomplete configurations
- **Rollback capability**: Safe migration with ability to revert changes

### Format Unification
- **Terminology standardization**: Converts cpvs to povs consistently
- **Schema normalization**: Ensures all outputs follow unified specification
- **Feature consolidation**: Merges best features from all input formats
- **Backward compatibility**: Maintains all functionality from source formats

### Batch Processing
- **Directory scanning**: Automatic discovery of benchmark projects
- **Bulk migration**: Process multiple benchmarks simultaneously
- **Progress tracking**: Real-time status updates and completion reporting
- **Parallel processing**: Efficient handling of large benchmark collections

### Validation and Quality Assurance
- **Schema validation**: Automatic checking against unified specification
- **Completeness verification**: Ensures all POVs and harnesses are migrated
- **Integrity checks**: Validates file references and path specifications
- **Migration reports**: Detailed summaries of changes and transformations

## State Management

The LangGraph system maintains comprehensive state throughout migration:

### Migration State
```python
@dataclass
class MigrationState:
    source_path: str
    source_format: str  # "internal" | "official"
    extracted_data: Dict[str, Any]
    converted_config: Dict[str, Any]
    validation_results: List[ValidationResult]
    output_path: str
    status: str  # "pending" | "analyzing" | "converting" | "validating" | "complete" | "failed"
    errors: List[str]
    warnings: List[str]
```

### Workflow Tracking
- **Step-by-step progress**: Track completion of each migration phase
- **Error recovery**: Handle failures at any stage with appropriate recovery
- **Audit trail**: Complete log of all transformations and decisions
- **Performance metrics**: Track migration speed and resource usage

## Configuration

### Agent Configuration
- **Model selection**: Choose appropriate LLM for different agents
- **Processing parameters**: Configure analysis depth and conversion strategies
- **Validation rules**: Customize validation strictness and requirements
- **Output formatting**: Control generated file structure and naming

### Migration Policies
- **Conflict resolution**: Handle conflicting configurations intelligently
- **Data prioritization**: Choose authoritative sources for conflicting data
- **Format preferences**: Specify preferred formats for ambiguous cases
- **Quality thresholds**: Set minimum requirements for successful migration

## Error Handling

### Graceful Degradation
- **Partial migration**: Handle incomplete source configurations
- **Missing data recovery**: Attempt to infer missing required fields
- **Format adaptation**: Handle non-standard format variations
- **User intervention**: Request human input for ambiguous cases

### Validation Failures
- **Schema violations**: Detailed reporting of specification mismatches
- **Data consistency**: Check for logical inconsistencies in configurations
- **File integrity**: Verify all referenced files exist and are accessible
- **Migration completeness**: Ensure no data loss during conversion

## Extensibility

### Plugin Architecture
- **Custom agents**: Add specialized agents for specific migration needs
- **Format handlers**: Support additional input and output formats
- **Validation rules**: Extend validation with domain-specific checks
- **Transformation pipelines**: Customize data transformation workflows

### Integration Points
- **CI/CD integration**: Automated migration in continuous integration
- **API endpoints**: Programmatic access to migration functionality
- **Webhook notifications**: Real-time updates on migration progress
- **External tool integration**: Connect with other CRS toolchains

## Future Enhancements

### Advanced Features
- **Incremental migration**: Update existing migrations with new source changes
- **Diff-based migration**: Show exactly what changes during migration
- **Template-based generation**: Use templates for consistent output formatting
- **Migration analytics**: Track migration patterns and success rates

### AI Improvements
- **Learning from corrections**: Improve agent performance from user feedback
- **Custom model fine-tuning**: Train specialized models for migration tasks
- **Context-aware decisions**: Better understanding of benchmark semantics
- **Automated quality improvement**: Suggest improvements to source configurations

## Dependencies

### Core Dependencies
- **langgraph**: Multi-agent workflow orchestration
- **langchain**: LLM integration and agent framework
- **pyyaml**: YAML parsing and generation
- **pydantic**: Data validation and schema management

### Optional Dependencies
- **click**: Command-line interface (if CLI is implemented)
- **rich**: Enhanced terminal output and progress bars
- **jinja2**: Template-based configuration generation
- **gitpython**: Git integration for version control operations