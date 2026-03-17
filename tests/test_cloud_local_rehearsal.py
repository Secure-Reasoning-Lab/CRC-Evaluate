"""Tests for local Docker rehearsal assets for cloud startup scripts."""

from __future__ import annotations

import base64
import json


def test_build_local_rehearsal_layout_writes_file_backed_metadata(tmp_path) -> None:
    """Local rehearsal should materialize metadata trees consumable by startup scripts."""
    from crsbench.cloud.local_rehearsal import build_local_rehearsal_layout

    config_path = tmp_path / "experiment.yaml"
    config_path.write_text(
        """
experiment: local-cloud-rehearsal
task: bugfinding
benchmark_suite: sanity
mode: delta
runtime:
  trials: 1
  max_total_time: 600
  build_timeout: 60
  run_timeout: 60
  verify_timeout: 60
  redis_host: ignored:6379
  litellm:
    skip: true
  inputs:
    pov:
      max_variants_per_cpv: 1
storage:
  experiment_filestore: /tmp/crsbench/experiment-data
  report_filestore: /tmp/crsbench/report-data
resources:
  cores_per_trial: 2
  memory_per_trial: 4G
worker:
  jobs: 1
  cores_per_job: 2
crs_compose:
  oss_crs_infra:
    shared: true
  atlantis-multilang-given_fuzzer:
    num_cores: 2
""".strip(),
        encoding="utf-8",
    )

    layout = build_local_rehearsal_layout(
        output_dir=tmp_path / "rehearsal",
        experiment_config_path=config_path,
        repo_mount_path="/src/CRSBench",
        worker_count=2,
    )

    orchestrator_root = layout.orchestrator_metadata_dir
    assert (orchestrator_root / "attributes" / "crsbench-install-spec").read_text(
        encoding="utf-8"
    ) == "git+file:///src/CRSBench"
    assert (orchestrator_root / "zone").read_text(encoding="utf-8") == "local-docker-a"

    worker_root = layout.worker_metadata_dirs[0]
    payload = json.loads(
        base64.b64decode(
            (worker_root / "attributes" / "crsbench-bootstrap-payload").read_text(
                encoding="utf-8"
            )
        ).decode("utf-8")
    )
    assert payload["redis_host"] == "orchestrator:6379"
    assert payload["benchmark_suite"] == "sanity"
    assert payload["prepare_mode"] == "full"
    assert payload["download_benchmarks"] == "auto"
    assert (worker_root / "id").read_text(encoding="utf-8").startswith("local-worker-")
