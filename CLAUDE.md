# Instructions
This repository is a benchmark to evaluate CRS (Cyber Reasoning System).
Benchmark is consists of a set of projects.

## Proposed RFC
The proposed standard for CRS benchmark is in `docs/benchmark-spec.md`.

## Glossary
- CRS: cyber reasoning system
- LLM: large language model
- POV: proof of vulnerability
- CP: challenge project

## Projects
- Each project is a directory under `benchmarks`.
- The project is Google OSS-Fuzz compatible.
- In each project, `project.yaml` is used to specify a project.
  - which programming language (e.g, C/C++, Java, Go, Rust)
  - which fuzzing engines (e.g., libfuzzer, AFL)
  - which sanitizers (e.g, ASAN, MSAN, UBSAN, Jazzer sanitizer)

## CRS Benchmark
There is an `.aixcc` directory under each project, which is used to store the
metadata of the benchmark and ground truth for each vulnerability.

## Old format used for internal usage
The current project use old format for internal testing when developing CRS.
We would like standardize it by enhancing it.

Example project is in `benchmarks-internal/r3_5-binutils`.

## Official AIxCC Benchmark for CRS.
AIxCC organizer also design a benchmark used to evaluate each team's CRS.
We would like to build on top of that and improve them.
Therefore, the official benchmark is provided in directory `benchmark-afc`.
The goal is to find a superset of features between ours and official one to
define a new standard for CRS benchmark and migrate them to the new standard.

Example project is in `benchmarks-afc/official-afc-systemd`.

## Comparison against FuzzBench
Unlike FuzzBench used to evaluate fuzzer, which only reports the
coverage/crashes.

CRSBench also stores the ground truth to catch whether a bug (POV) is actually
found or missed.

On top of that, we also provide basic infrastructure like LiteLLM to support the
need of LLM used in modern AI-powered CRS.


## Other instructions
