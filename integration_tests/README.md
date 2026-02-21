# Integration Tests

Local integration test scripts and configs for CRSBench.

## Structure

- `run_local_test.sh`: canonical local integration runner
- `run_local_test-*.sh`: thin scenario wrappers around `run_local_test.sh`
- `test-experiment-config*.yaml`: scenario configs used by the wrappers

## Usage

Run default scenario:

```bash
integration_tests/run_local_test.sh
```

Run a specific config directly:

```bash
integration_tests/run_local_test.sh --config test-experiment-config-libfuzzer.yaml --gitcache
```

Use wrapper shortcuts:

```bash
integration_tests/run_local_test-libfuzzer.sh
integration_tests/run_local_test-patch-vincent.sh
```
