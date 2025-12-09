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

- [x]  Implement experiment scripts and configurations (e.g., run_experiment.py)
- [x]  job queue and worker (like fuzzbench with redis)
- [x]  CRS interface
    - [x] bug-finding
    - [x] patch
    - [x] make sure them could be run from any cwd
    - [ ] everything CRSBench need to pass to them are configurable
- [ ]  Wrapper codes for running bug finding CRS
    - [x]  Mock CRS bug-finding interface
    - [x]  Agreement with CRS standardization team
- [ ]  Wrapper codes for running patching CRS
    - [x]  Mock CRS patching interface
    - [x]  Agreement with CRS standardization team / oss-patch
    - [ ]  pass `patch_exclude_list` or not?
- [ ] passing hints
  - [ ] corpus (for oss-patch too?)
  - [ ] SARIF
- [ ] passing POVs to oss-patch
  - [ ] how many POVs? (all? hidden POVs?)
- [ ]  end-to-end test with run_experiment.py
    - [ ]  local test
        - [x]  one job
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
- [ ] mount harness source code in container (hint or always available?)
  - [ ] parse $PROJECT/$REPO and replace with host path
  - [ ] decide filename and path in container

## Bug-finding and Patch Validation

- [ ]  extract POVs/patches from (incremental) snapshots
- [ ]  sanitizers? (other than ASAN?)
- [ ]  timeout bugs like AIxCC?
- [ ]  Bug finding validation
    - [ ]  PoV deduplication
    - [ ]  stability test (e.g., crash 10/10 times)
- [ ]  Patch validation
    - [ ]  stability test (e.g., no crash 10/10 times)
    - [ ]  `patch_exclude_list` checking.
    - [ ]  patch validation methods (check crete, AutoPatchBench)
      - [ ] how many/all POVs are killed.
      - [ ] fuzzing after patch for spurious crashes.
      - [ ] differential fuzzing.
      - [ ] delta debugging.
      - [ ] invariant checking (e.g. unit tests)
    - [ ]  Develop LLM agent for dual-phase build script generation []
    - [ ]  Incremental building; generate incremental build scripts (build-pre.sh, build-apply.sh) for each project
        - [ ]  Test on curl-delta-02 and wireshark [@Youngjoon]
    - [ ]  Incremental building; generate Dockerfile for each patch & build
    - [ ]  Implement selective unit test

## LLM setup Logging
- [ ] liteLLM setup
  - [ ] all available models & total budgets
  - [x] master key? CRS can allocate budgets for models by themselves
  - [ ] each trial (CRS, benchmark, trial_id) should have its own liteLLM
        instance to prevent conflict. Assume that multiple trials on the same
        host machine.
- [ ] [liteLLM logging](https://docs.litellm.ai/docs/proxy/logging)
We need a format that can be easier snapshotted. Better just a file..

## Reporting

- [ ]  HTML format report generation (+ visualize)
- [ ]  report from validation results & snapshots
- [ ]  CRS-specific reports
