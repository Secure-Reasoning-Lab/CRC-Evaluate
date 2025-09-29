# CRSBench Hint Generation Module

## Overview

The hint generation module provides an intelligent, AI-powered system for automatically creating progressive vulnerability hints from Proof of Vulnerability (POV) data. This module uses specialized agents to analyze crash logs, code patches, and vulnerability information to generate multi-level hints that control benchmark difficulty and guide CRS systems toward vulnerability discovery.

## Purpose

This module addresses the need to automatically generate educational hints that help CRS systems learn and discover vulnerabilities at appropriate difficulty levels. By analyzing existing POV data, it creates progressive hint sequences that provide increasingly specific guidance about vulnerabilities.

### Hint-Based Difficulty Control

According to the CRSBench specification, hints create multiple difficulty dimensions:
- **No Hints + No POVs**: Maximum difficulty - pure vulnerability discovery
- **Basic Hints + No POVs**: High difficulty - guided discovery without crash examples
- **Detailed Hints + Multiple POVs**: Lower difficulty - comprehensive guidance with crash data
- **Progressive Hints**: CRS can request increasingly specific hints with cost penalties

## Architecture

### Multi-Agent LangGraph System

The hint generation system employs a specialized workflow with AI agents that understand vulnerability patterns:

```
POV Analysis → Code Analysis → Hint Generation → Quality Validation → Progressive Refinement
```

### Core Agents

#### 1. **POV Analysis Agent**
- **Purpose**: Deep analysis of proof of vulnerability artifacts
- **Capabilities**:
  - Parse sanitizer crash logs (AddressSanitizer, MemorySanitizer, etc.)
  - Extract error patterns and memory corruption signatures
  - Analyze crash stack traces for vulnerability location
  - Identify vulnerability categories from error tokens
  - Map crash symptoms to vulnerability types

#### 2. **Code Analysis Agent**
- **Purpose**: Analyze vulnerable code and patches for context
- **Capabilities**:
  - Parse patch files to understand vulnerability fixes
  - Identify vulnerable functions and code patterns
  - Extract file paths, line numbers, and function names
  - Understand code context around vulnerabilities
  - Analyze complexity of vulnerability exploitation

#### 3. **Hint Generation Agent**
- **Purpose**: Create progressive hint sequences from analyzed data
- **Capabilities**:
  - Generate Level 1 hints: General vulnerability category
  - Generate Level 2 hints: Specific vulnerability type and location
  - Generate Level 3 hints: Function-level guidance
  - Generate Level 4 hints: Line-specific implementation details
  - Ensure hint progression from general to specific

#### 4. **Quality Control Agent**
- **Purpose**: Validate and refine generated hints
- **Capabilities**:
  - Verify hint accuracy against POV data
  - Ensure proper difficulty progression
  - Check for information leakage between levels
  - Validate technical correctness
  - Optimize hint clarity and usefulness

## Hint Generation Workflow

### Stage 1: POV Data Ingestion
- Process POV artifacts (crash logs, patches, blobs)
- Extract vulnerability signatures and patterns
- Identify sanitizer error types and locations
- Build comprehensive vulnerability profile

### Stage 2: Vulnerability Classification
- Categorize vulnerability type (buffer overflow, use-after-free, etc.)
- Determine severity level and exploitation potential
- Identify affected components and code modules
- Assess complexity of vulnerability discovery

### Stage 3: Progressive Hint Creation
- **Level 1**: Generate broad vulnerability category hints
- **Level 2**: Create specific type and location hints
- **Level 3**: Develop function-level guidance
- **Level 4**: Provide precise implementation hints

### Stage 4: Quality Assurance
- Validate hint accuracy and progression
- Ensure no premature information disclosure
- Test hint effectiveness for CRS guidance
- Refine hints based on quality metrics

## Input Data Sources

### POV Artifacts
```yaml
# POV structure from benchmarks
povs:
  - name: "pov_0"
    sanitizer: "address"
    error_token: "AddressSanitizer: heap-buffer-overflow"
    requires_clean_build: true
```

### Supporting Files
- **Crash logs**: Sanitizer output with stack traces
- **Patch files**: Code fixes showing vulnerability location
- **Binary blobs**: Input data that triggers crashes
- **Vulnerability descriptions**: Human-readable explanations

### Code Context
- Source code files around vulnerability
- Function implementations and call graphs
- Build system and compilation context
- Test harness specifications

## Output Format

### Progressive Hint Structure

Based on the CRSBench RFC specification, hints follow this format:

```yaml
hints:
  - level: 1
    text: "Memory safety issue detected"
    category: "vulnerability_type"

  - level: 2
    text: "Buffer overflow in input processing"
    category: "location"

  - level: 3
    text: "Missing bounds check in parse_input() function"
    category: "technical"

  - level: 4
    text: "Line 245: buffer[i] = input[i] without length validation"
    category: "implementation"
```

### Hint Categories

#### Vulnerability Category Hints (Level 1)
- "Memory safety issue present"
- "Use-after-free vulnerability"
- "Integer overflow detected"
- "Race condition vulnerability"

#### Location Hints (Level 2)
- "Vulnerability in parsing module"
- "Issue in network handling functions"
- "Buffer overflow in input processing"
- "Memory corruption in data structures"

#### Technical Hints (Level 3)
- "Missing bounds checking"
- "Unvalidated input processing"
- "Improper memory management"
- "Race condition in threading code"

#### Implementation Hints (Level 4)
- Specific line numbers and code patterns
- Exact function names and parameters
- Precise variable names and operations
- Detailed fix implementations

## Agent Capabilities

### Vulnerability Pattern Recognition
- **Buffer Overflows**: Detect bounds checking issues from crash patterns
- **Use-After-Free**: Identify memory lifecycle violations
- **Integer Overflows**: Recognize arithmetic operation issues
- **Race Conditions**: Find concurrency-related vulnerabilities

### Code Understanding
- **Static Analysis**: Parse source code for vulnerability patterns
- **Dynamic Analysis**: Understand crash behavior from sanitizer logs
- **Patch Analysis**: Extract fixes to understand root causes
- **Context Awareness**: Maintain understanding across files and functions

### Hint Optimization
- **Difficulty Calibration**: Adjust hint specificity for target difficulty
- **Information Control**: Prevent excessive hint disclosure
- **Progressive Revelation**: Ensure logical hint sequence progression
- **Educational Value**: Maximize learning potential for CRS systems

## Quality Metrics

### Hint Accuracy
- **Technical Correctness**: Verify hints match actual vulnerabilities
- **Location Precision**: Ensure hints point to correct code locations
- **Category Classification**: Validate vulnerability type assignments
- **Implementation Details**: Check accuracy of specific code references

### Progression Quality
- **Logical Flow**: Ensure hints build naturally from general to specific
- **Information Leakage**: Prevent lower levels from revealing higher-level details
- **Difficulty Gradient**: Maintain appropriate challenge levels
- **Completeness**: Cover all aspects of vulnerability understanding

### CRS Effectiveness
- **Discovery Guidance**: Measure how well hints guide vulnerability finding
- **Learning Enhancement**: Assess educational value for CRS improvement
- **Cost-Benefit Analysis**: Optimize hint utility vs. difficulty reduction
- **Success Correlation**: Track hint usage patterns and discovery rates

## Configuration Options

### Generation Parameters
```yaml
hint_generation:
  max_levels: 4                    # Number of hint levels to generate
  categories: ["type", "location", "technical", "implementation"]
  detail_threshold: 0.8            # Specificity control
  context_window: 10               # Lines of code context

quality_control:
  accuracy_threshold: 0.95         # Minimum hint accuracy
  progression_check: true          # Validate hint progression
  information_leakage_detection: true

output_format:
  include_metadata: true           # Add generation metadata
  hint_categories: true            # Include category labels
  confidence_scores: false         # Skip confidence ratings
```

### Agent Behavior
- **Analysis Depth**: Control thoroughness of POV analysis
- **Code Context**: Specify amount of surrounding code to analyze
- **Hint Granularity**: Adjust specificity of generated hints
- **Quality Thresholds**: Set minimum standards for hint acceptance

## Integration with Benchmark System

### Automatic Hint Generation
- **Batch Processing**: Generate hints for entire benchmark suites
- **Incremental Updates**: Add hints to existing benchmarks
- **Format Integration**: Output hints in CRSBench YAML format
- **Validation Pipeline**: Ensure hints meet specification requirements

### Difficulty Control Integration
```yaml
# Generated hints integrated into benchmark configuration
harness_files:
  - name: "fuzz_parser"
    path: "$REPO/test/fuzz_parser.c"
    povs:
      - name: "buffer_overflow_main"
        sanitizer: "address"
        error_token: "AddressSanitizer: heap-buffer-overflow"
        # Auto-generated progressive hints
        hints:
          - level: 1
            text: "Memory safety issue detected"
          - level: 2
            text: "Buffer overflow in parsing logic"
          - level: 3
            text: "Missing bounds check in parse_header() function"
          - level: 4
            text: "Line 142: memcpy(buf, input, len) without length validation"
```

### Benchmark Customization
- **Difficulty Targeting**: Generate hints for specific difficulty levels
- **Educational Sequences**: Create learning progressions for CRS training
- **Adaptive Hints**: Adjust hint content based on CRS performance
- **Contextual Guidance**: Provide hints relevant to specific code patterns

## State Management

The LangGraph system maintains comprehensive state throughout hint generation:

### Generation State
```python
@dataclass
class HintGenerationState:
    pov_data: Dict[str, Any]           # Input POV artifacts
    vulnerability_analysis: VulnAnalysis
    code_context: CodeContext
    generated_hints: List[Hint]
    quality_scores: Dict[str, float]
    validation_results: ValidationResult
    status: str                        # "analyzing" | "generating" | "validating" | "complete"
    errors: List[str]
    metadata: Dict[str, Any]
```

### Quality Tracking
- **Generation Metrics**: Track time, resources, and success rates
- **Accuracy Assessment**: Monitor hint correctness and relevance
- **Usage Analytics**: Measure hint effectiveness in CRS evaluations
- **Improvement Feedback**: Collect data for agent optimization

## Error Handling and Robustness

### Input Validation
- **POV Data Integrity**: Verify crash logs and patch file validity
- **Code Accessibility**: Ensure source code is available and parseable
- **Format Compatibility**: Handle various sanitizer output formats
- **Missing Data Recovery**: Generate hints even with incomplete POV data

### Generation Failures
- **Partial Hint Generation**: Provide available hints when full sequence fails
- **Quality Degradation**: Accept lower quality hints when necessary
- **Fallback Strategies**: Use template-based hints for difficult cases
- **Error Reporting**: Provide detailed diagnostics for generation failures

### Validation and Correction
- **Automated Correction**: Fix common hint generation errors
- **Human Review Integration**: Support manual hint validation workflows
- **Iterative Improvement**: Refine hints based on usage feedback
- **Version Management**: Track hint versions and improvements

## Future Enhancements

### Advanced AI Capabilities
- **Multi-Modal Analysis**: Combine code, binary, and dynamic analysis
- **Cross-Vulnerability Learning**: Learn patterns across multiple POVs
- **Adaptive Generation**: Customize hints based on CRS learning patterns
- **Automated Evaluation**: Self-assess hint quality and effectiveness

### Educational Features
- **Learning Pathways**: Create structured vulnerability discovery curricula
- **Concept Mapping**: Link related vulnerabilities and patterns
- **Interactive Hints**: Provide dynamic hints based on CRS progress
- **Knowledge Assessment**: Test CRS understanding of hinted concepts

### Integration Expansions
- **IDE Integration**: Provide hints directly in development environments
- **Real-time Generation**: Create hints for new vulnerabilities on-demand
- **Collaborative Filtering**: Share and improve hints across CRS teams
- **Benchmark Synthesis**: Generate entire benchmarks from hint specifications

## Dependencies

### Core Dependencies
- **langgraph**: Multi-agent workflow orchestration
- **langchain**: LLM integration and agent framework
- **pyyaml**: YAML parsing and generation
- **pydantic**: Data validation and schema management

### Analysis Dependencies
- **tree-sitter**: Code parsing and AST generation
- **pygments**: Syntax highlighting and code analysis
- **clang-python**: C/C++ code analysis bindings
- **regex**: Advanced pattern matching for log analysis

### Optional Dependencies
- **matplotlib**: Visualization of hint quality metrics
- **networkx**: Code dependency graph analysis
- **scikit-learn**: Machine learning for hint optimization
- **jupyter**: Interactive hint development and testing