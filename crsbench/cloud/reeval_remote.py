"""Remote wrapper for cloud re-eval submissions."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import yaml

_STATE_FILENAME = "submission.json"
_SUMMARY_FILENAME = "summary.json"
_LOG_FILENAME = "runner.log"


def read_submission_state(state_path: Path) -> dict[str, Any]:
    """Load one submission state file."""
    return json.loads(state_path.read_text(encoding="utf-8"))


def write_submission_state(state_path: Path, payload: dict[str, Any]) -> None:
    """Persist submission state atomically."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=state_path.parent,
            prefix=f"{state_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(payload, handle, indent=2)
            temp_path = Path(handle.name)
        temp_path.replace(state_path)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def launch_remote_submission(
    submission_dir: Path,
    *,
    redis_host: str = "localhost:6379",
    benchmarks_root: Path | None = None,
) -> int:
    """Mark a bundle published and start detached execution."""
    state_path = submission_dir / _STATE_FILENAME
    state = read_submission_state(state_path)
    state["state"] = "published"
    state["published_at"] = _now_iso()
    state["redis_host"] = redis_host
    if benchmarks_root is not None:
        state["benchmarks_root"] = str(benchmarks_root)
    write_submission_state(state_path, state)

    log_handle = (submission_dir / _LOG_FILENAME).open("a", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "crsbench.cloud.reeval_remote",
            "execute",
            "--submission-dir",
            str(submission_dir),
        ],
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return process.pid


def execute_remote_submission(
    submission_dir: Path,
    *,
    redis_host: str | None = None,
    benchmarks_root: Path | None = None,
) -> int:
    """Materialize a workspace from a published bundle and run `crsbench re-eval`."""
    state_path = submission_dir / _STATE_FILENAME
    state = read_submission_state(state_path)
    bundle_dir = Path(state.get("bundle_path") or submission_dir / "bundle")
    workspace_dir = Path(state.get("workspace_path") or submission_dir / "workspace")

    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    remote_experiment_name = manifest["remote_experiment_name"]
    workspace_experiment_dir = workspace_dir / remote_experiment_name

    state["state"] = "materializing"
    state["materializing_at"] = _now_iso()
    state["workspace_path"] = str(workspace_dir)
    write_submission_state(state_path, state)

    workspace_dir.mkdir(parents=True, exist_ok=True)
    workspace_experiment_dir.mkdir(parents=True, exist_ok=True)
    _materialize_trials(
        bundle_dir=bundle_dir,
        manifest=manifest,
        workspace_experiment_dir=workspace_experiment_dir,
    )

    derived_config = _build_derived_config(
        source_config_path=bundle_dir / "config" / "source-config.yaml",
        remote_experiment_name=remote_experiment_name,
        experiment_filestore=workspace_dir,
        redis_host=redis_host or state.get("redis_host") or "localhost:6379",
        benchmarks_root=benchmarks_root or _maybe_path(state.get("benchmarks_root")),
    )
    derived_config_path = workspace_dir / "experiment-config.yaml"
    derived_config_path.write_text(
        yaml.safe_dump(derived_config, sort_keys=False),
        encoding="utf-8",
    )

    state["state"] = "running"
    state["started_at"] = _now_iso()
    write_submission_state(state_path, state)

    result = subprocess.run(
        [
            "crsbench",
            "re-eval",
            "--experiment-config",
            str(derived_config_path),
        ],
        check=False,
    )
    terminal_state = "succeeded" if result.returncode == 0 else "failed"
    state["state"] = terminal_state
    state["completed_at"] = _now_iso()
    state["reeval_exit_code"] = result.returncode
    write_submission_state(state_path, state)

    summary = {
        "bundle_id": manifest["bundle_id"],
        "source_experiment_name": manifest["source_experiment_name"],
        "remote_experiment_name": manifest["remote_experiment_name"],
        "selected_trial_count": manifest["selected_trial_count"],
        "skipped_trial_count": manifest["skipped_trial_count"],
        "started_at": state.get("started_at"),
        "completed_at": state.get("completed_at"),
        "state": terminal_state,
        "reeval_exit_code": result.returncode,
    }
    (submission_dir / _SUMMARY_FILENAME).write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )
    return result.returncode


def _materialize_trials(
    *,
    bundle_dir: Path,
    manifest: dict[str, Any],
    workspace_experiment_dir: Path,
) -> None:
    for trial in manifest.get("selected_trials", []):
        relative_path = Path(trial["relative_path"])
        shutil.copytree(
            bundle_dir / "trials" / relative_path,
            workspace_experiment_dir / relative_path,
            dirs_exist_ok=True,
        )


def _build_derived_config(
    *,
    source_config_path: Path,
    remote_experiment_name: str,
    experiment_filestore: Path,
    redis_host: str,
    benchmarks_root: Path | None,
) -> dict[str, Any]:
    raw_config = yaml.safe_load(source_config_path.read_text(encoding="utf-8"))
    if not isinstance(raw_config, dict):
        raise ValueError(f"Expected YAML mapping in {source_config_path}")

    raw_config["experiment"] = remote_experiment_name
    raw_config["experiment_filestore"] = str(experiment_filestore)
    raw_config["redis_host"] = redis_host

    runtime = raw_config.setdefault("runtime", {})
    if not isinstance(runtime, dict):
        raise ValueError("Experiment config 'runtime' section must be a mapping")
    runtime_redis = runtime.setdefault("redis", {})
    if not isinstance(runtime_redis, dict):
        raise ValueError("Experiment config 'runtime.redis' section must be a mapping")
    runtime_redis["host"] = redis_host

    if benchmarks_root is not None:
        raw_config["benchmarks_root"] = str(benchmarks_root)
    return raw_config


def _maybe_path(value: Any) -> Path | None:
    if isinstance(value, str) and value.strip():
        return Path(value)
    return None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Internal cloud re-eval remote runner")
    subparsers = parser.add_subparsers(dest="command", required=True)

    launch_p = subparsers.add_parser("launch")
    launch_p.add_argument("--submission-dir", type=Path, required=True)
    launch_p.add_argument("--redis-host", default="localhost:6379")
    launch_p.add_argument("--benchmarks-root", type=Path, default=None)

    execute_p = subparsers.add_parser("execute")
    execute_p.add_argument("--submission-dir", type=Path, required=True)
    execute_p.add_argument("--redis-host", default=None)
    execute_p.add_argument("--benchmarks-root", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "launch":
        launch_remote_submission(
            args.submission_dir,
            redis_host=args.redis_host,
            benchmarks_root=args.benchmarks_root,
        )
        return 0
    return execute_remote_submission(
        args.submission_dir,
        redis_host=args.redis_host,
        benchmarks_root=args.benchmarks_root,
    )


if __name__ == "__main__":
    raise SystemExit(main())
