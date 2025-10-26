# CRSBench TODO List

## Benchmark Converting [@Youngjoon, @Dongkwan, @Jiho]

- [x]  Define RFC format using json schema or Pydantic [@Yufu]
- [x]  Automatically convert Team-Atlanta benchmarks to RFC format [@Youngjoon]
- [x]  Add pov-variants into the repo [@Dongkwan]
- [x]  Add patch-variants into the repo [@Youngjoon]
- [ ]  Define and assign difficulty level based on results of naive fuzzer or multilang [@Jiho]
- [ ]  Corpus collecting; also "corpus-[timestamp]" for 1h/1d pre-fuzzing corpus. [@Jiho]
- [x]  Make hint generation (vuln.yaml generation) LLM-agent [@Youngjoon]
- [x]  Generate hints in natural language [@Youngjoon]
- [x]  Convert hints to SARIF format [@Youngjoon]
- [ ]  Create `test.sh` if missing [@all]
- [ ]  Manual review and finalize each benchmark entries [@all]
- [x]  Change `repo_url` of r2 and r3 benchmarks to use afc one [@Youngjoon]

## Running RFC-compliant CRSes [@Yufu]

- [ ]  Implement experiment scripts and configurations (e.g., run_experiment.py)
- [x]  job queue and worker (like fuzzbench with redis)
- [x]  CRS interface
    - [x] bug-finding
    - [x] patch
- [ ]  Wrapper codes for running bug finding CRS
    - [ ]  Mock CRS bug-finding interface
    - [ ]  Agreement with CRS standardization team
- [ ]  Wrapper codes for running patching CRS
    - [ ]  Mock CRS patching interface
    - [ ]  Agreement with CRS standardization team / oss-patch
- [ ]  end-to-end test with run_experiment.py
    - [ ]  local test
        - [ ]  one job
        - [ ]  multiple jobs
            - [ ]  proper way to find a reasonable number for concurrent jobs for a machine or user-defined
            - [ ]  make sure no filesystem conflicts & severe resource contention
    - [ ]  distributed test
- [ ] fault tolerance (e.g. CRS exits/killed, timeout handling)
- [ ] snapshots
  - [x] take snapshots
  - [x] snapshot example
  - [x] inspect/list snapshot contents
  - [ ] mock bug/patch validation interface to test snapshots
  - [ ] mock interface for reports
- [ ]  CRS-specific analysis scripts (their own crs-data)
- [ ] build/health check for CRS images before evaluation

## Bug-finding and Patch Validation

- [ ]  Bug finding validation
    - [ ]  PoV deduplication
    - [ ]  stability test (e.g., crash 10/10 times)
- [ ]  Patch validation
    - [ ]  stability test (e.g., no crash 10/10 times)
    - [ ]  patch validation methods (check crete, AutoPatchBench)
      - [ ] how many/all POVs are killed.
      - [ ] fuzzing after patch for spurious crashes.
      - [ ] differential fuzzing.
      - [ ] delta debugging.
    - [ ]  Develop LLM agent for dual-phase build script generation []
    - [ ]  Incremental building; generate incremental build scripts (build-pre.sh, build-apply.sh) for each project
        - [ ]  Test on curl-delta-02 and wireshark [@Youngjoon]
    - [ ]  Incremental building; generate Dockerfile for each patch & build
    - [ ]  Implement selective unit test

## Reporting

- [ ]  HTML format report generation (+ visualize)
- [ ]  report from validation results & snapshots
- [ ]  CRS-specific reports
