# Distributed Benchmark CI

Use this page for CI queue topology and evaluator deployment.

## Recommended Topology

- one submitter: `uv run crsbench benchmark ci ... --distributed`
- one evaluator machine: `uv run crsbench evaluator --ci ...`
- one shared Valkey/Redis instance

## Example

```bash
uv run python scripts/valkey-helper.py --password start

uv run crsbench evaluator --ci \
  --jobs 8 --cores-per-job 16 \
  --idle-timeout 0

uv run crsbench benchmark ci all --all \
  --distributed \
  --mode snapshot \
  --output-dir ./ci-output
```

## Notes

- Keep evaluator concurrency on the evaluator CLI, not on the submitter.
- Use one evaluator by default unless you intentionally partition queues.
- Build and verify artifacts are written under the CI output directory.

## Related

- Full CI reference: [reference.md](./reference.md)
- Experiment queue behavior: [../experiments/queue-and-recovery.md](../experiments/queue-and-recovery.md)
