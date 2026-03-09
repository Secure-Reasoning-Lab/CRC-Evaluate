# Distributed Benchmark CI

Use this page for CI queue topology and evaluator deployment.

## Recommended Topology

- one submitter: `crsbench benchmark ci ... --distributed`
- one evaluator machine: `crsbench evaluator --ci ...`
- one shared Valkey/Redis instance

## Example

```bash
python scripts/valkey-helper.py --password start

crsbench evaluator --ci \
  --build-jobs 8 --build-cores-per-job 16 \
  --verify-jobs 8 --verify-cores-per-job 16 \
  --idle-timeout 0

crsbench benchmark ci all --all \
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
