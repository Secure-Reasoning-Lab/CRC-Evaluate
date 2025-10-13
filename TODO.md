# CRSBench TODO List

## Discussion 10/07
- [x]  Implement converting script from AFC format to RFC format (Jiho has already convert AFC benchmarks into our format)
- [ ]  Converting script from Team-Atlanta format to RFC format
- [ ]  Wrapper codes for running bug finding CRS
- [ ]  Wrapper codes for running patching CRS
- [ ]  Implement experiment scripts
- [ ]  Bug finding validation
    - [ ]  PoV deduplication
- [ ]  Patch validation
    - [ ]  Build incremental build script for each project (should we add it to oss-patch instead of benchmark??)
    - [ ]  incremental building; generate Dockerfile for each patch & build
    - [ ]  Implement selective unit test (should we add it to oss-patch instead of benchmark??)
- [ ]  Assign ppl to review the each benchmark
    - [ ]  Check the automatically converted format
    - [ ]  Adding hints as a SARIF format
- [ ]  assign difficulty level (maybe after running first evals?)
- [ ]  Implement CI/CD??
    - [ ] automatic meta.yaml generation with commit range & ref.diff
- [ ] visualize & sample report & ranking
- [ ] meta.yaml
    - store absolute path for fuzzing harness source code in container instead
      of $REPO/$PROJECT
    - remove redundant "[pov-keyword]/[pov-keyword].{blog,log}"
    - where to store blob/log (git LFS?)
- [ ] corpus
     - other than level; also "corpus-[timestamp]" for 1h/1d pre-fuzzing corpus.
     - where to store corpus (git LFS?)

Kudu opinions:
consider other than bug finding/patching => e.g., confidential leaking
difficulty level changes when LLM become smarter??




## High Priority

### 🔄 Migration Implementation
- [ ] **Migration Agent Implementation**
  - [ ] Create LangGraph workflow for migration
  - [ ] Implement format detection agent
  - [ ] Implement analysis agent for old formats
  - [ ] Implement conversion agent (internal → unified)
  - [ ] Implement conversion agent (official AIxCC → unified)
  - [ ] Add validation integration to migration workflow
  - [ ] Test migration with `benchmarks-internal/r3_5-binutils`
  - [ ] Test migration with `benchmarks-afc/official-afc-systemd`

### 💡 Hint Generation Implementation
- [ ] **Hint Generation Agent Implementation**
  - [ ] Create LangGraph workflow for hint generation
  - [ ] Implement POV analysis agent (crash log parsing)
  - [ ] Implement code analysis agent (patch analysis)
  - [ ] Implement hint generation agent (4-level hints)
  - [ ] Implement quality control agent
  - [ ] Add validation integration
  - [ ] Test hint generation with existing POVs

### 🛠️ Utilities Implementation
- [ ] **Shared Utilities**
  - [ ] YAML handling utilities (`crsbench/utils/yaml_handler.py`)
  - [ ] File operations utilities (`crsbench/utils/file_ops.py`)
  - [ ] Configuration management (`crsbench/utils/config.py`)
  - [ ] Logging setup (`crsbench/utils/logging.py`)
  - [ ] Path utilities for benchmark navigation

## Medium Priority

### 🧪 Testing & Validation
- [ ] **Test Suite Creation**
  - [ ] Unit tests for validation module
  - [ ] Integration tests for migration workflows
  - [ ] Test fixtures with sample benchmarks
  - [ ] Performance tests for large benchmark suites
  - [ ] Error handling tests

### 🎯 Benchmark Migration
- [ ] **Format Standardization**
  - [ ] Migrate all `benchmarks-internal/` projects
  - [ ] Migrate all `benchmarks-afc/` projects
  - [ ] Validate all migrated benchmarks
  - [ ] Update documentation for new format
  - [ ] Archive old format documentation

### 🖥️ CLI Interface
- [ ] **Command Line Tools**
  - [ ] `crsbench validate <path>` - Validate benchmark
  - [ ] `crsbench migrate <source> <target>` - Migrate format
  - [ ] `crsbench generate-hints <benchmark>` - Generate hints
  - [ ] `crsbench info <benchmark>` - Show benchmark info
  - [ ] Progress bars and rich output with Rich library

## Low Priority

### 📊 Analysis & Reporting
- [ ] **Benchmark Analytics**
  - [ ] Generate benchmark statistics
  - [ ] Difficulty analysis across benchmark suite
  - [ ] POV distribution analysis
  - [ ] Harness complexity metrics
  - [ ] Migration success rate tracking

### 🌐 Web Interface
- [ ] **Dashboard Development**
  - [ ] Web-based benchmark browser
  - [ ] Validation results visualization
  - [ ] Migration progress tracking
  - [ ] Hint generation interface
  - [ ] Benchmark comparison tools

### 🔌 Integration & Extensions
- [ ] **External Integrations**
  - [ ] GitHub Actions workflow for CI/CD
  - [ ] Docker containers for evaluation
  - [ ] Integration with OSS-Fuzz infrastructure
  - [ ] Plugin system for custom validators
  - [ ] API endpoints for programmatic access

### 📚 Documentation & Community
- [ ] **Documentation Expansion**
  - [ ] Tutorial for creating new benchmarks
  - [ ] CRS evaluation guidelines
  - [ ] Best practices documentation
  - [ ] Video tutorials and demos
  - [ ] Community contribution guidelines

## Technical Debt & Improvements

### 🔧 Code Quality
- [ ] **Code Improvements**
  - [ ] Add type hints to all modules
  - [ ] Improve error messages
  - [ ] Add more comprehensive logging
  - [ ] Performance optimization
  - [ ] Memory usage optimization

### 🔒 Security & Robustness
- [ ] **Security Enhancements**
  - [ ] Input sanitization for all file operations
  - [ ] Safe YAML loading practices
  - [ ] Path traversal protection
  - [ ] Sandbox execution for untrusted code
  - [ ] Security audit of LLM integrations

## Future Enhancements

### 🤖 AI Improvements
- [ ] **Advanced AI Features**
  - [ ] Custom model fine-tuning for migration
  - [ ] Learning from migration feedback
  - [ ] Automated benchmark quality scoring
  - [ ] Intelligent benchmark categorization
  - [ ] Adaptive hint generation based on CRS performance

### 🎓 Educational Features
- [ ] **Learning & Training**
  - [ ] Interactive tutorial mode
  - [ ] Progressive difficulty challenges
  - [ ] CRS training datasets
  - [ ] Educational vulnerability examples
  - [ ] Hands-on cybersecurity workshops

### 🌍 Ecosystem Integration
- [ ] **Broader Integration**
  - [ ] Integration with other security tools
  - [ ] Support for binary-only challenges
  - [ ] Multi-language support (Java, Go, Rust)
  - [ ] Cloud-based evaluation platform
  - [ ] Competition hosting platform

## Notes

- **Priority Levels**: High (next sprint), Medium (next release), Low (future versions)
- **Dependencies**: Many items depend on completion of migration and hint generation agents
- **Resource Requirements**: Consider LLM API costs for agent-based features
- **Testing Strategy**: Implement comprehensive testing before production use

## Contributing

To contribute to any of these TODO items:

1. Check if the item is already being worked on
2. Create a new branch for your work
3. Update this TODO list to mark items as "In Progress"
4. Move completed items to `DONE.md`
5. Submit a pull request with your changes

---

**Last Updated**: 2025-01-XX
**Next Review**: Weekly during development phase
