"""Remote wrapper for cloud re-eval submissions."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import yaml

from crsbench.cloud.reeval_compat import discover_git_revision as _discover_git_revision

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
    state["redis_host"] = redis_host
    if benchmarks_root is not None:
        state["benchmarks_root"] = str(benchmarks_root)
    published_at = _now_iso()
    state["state"] = "published"
    state["published_at"] = published_at
    write_submission_state(state_path, state)
    with (submission_dir / _LOG_FILENAME).open("a", encoding="utf-8") as log_handle:
        try:
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
        except Exception as exc:
            failed_state = read_submission_state(state_path)
            failed_state["state"] = "failed"
            failed_state["completed_at"] = _now_iso()
            failed_state["runner_error"] = str(exc)
            failed_state["reeval_exit_code"] = 1
            write_submission_state(state_path, failed_state)
            _write_summary(
                submission_dir=submission_dir,
                manifest=None,
                state=failed_state,
                exit_code=1,
                error=str(exc),
            )
            raise

    latest_state = read_submission_state(state_path)
    latest_state.setdefault("published_at", published_at)
    if latest_state.get("state") == "uploading":
        latest_state["state"] = "published"
    latest_state["runner_pid"] = process.pid
    write_submission_state(state_path, latest_state)
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
    manifest: dict[str, Any] | None = None

    try:
        manifest = json.loads(
            (bundle_dir / "manifest.json").read_text(encoding="utf-8")
        )
        remote_experiment_name = manifest["remote_experiment_name"]
        workspace_experiment_dir = workspace_dir / remote_experiment_name
        effective_benchmarks_root = benchmarks_root or _maybe_path(
            state.get("benchmarks_root")
        )

        _validate_bundle_compatibility(
            manifest=manifest,
            benchmarks_root=effective_benchmarks_root,
        )

        state["state"] = "materializing"
        state["materializing_at"] = _now_iso()
        state["workspace_path"] = str(workspace_dir)
        write_submission_state(state_path, state)

        workspace_dir.mkdir(parents=True, exist_ok=True)
        if workspace_experiment_dir.exists():
            shutil.rmtree(workspace_experiment_dir)
        workspace_experiment_dir.mkdir(parents=True, exist_ok=False)
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
            benchmarks_root=effective_benchmarks_root,
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
                sys.executable,
                "-m",
                "crsbench.run_experiment",
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
        _write_summary(
            submission_dir=submission_dir,
            manifest=manifest,
            state=state,
            exit_code=result.returncode,
        )
        return result.returncode
    except Exception as exc:
        traceback.print_exc()
        state["state"] = "failed"
        state["completed_at"] = _now_iso()
        state["reeval_exit_code"] = 1
        state["error"] = str(exc)
        write_submission_state(state_path, state)
        _write_summary(
            submission_dir=submission_dir,
            manifest=manifest,
            state=state,
            exit_code=1,
            error=str(exc),
        )
        return 1


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


def _validate_bundle_compatibility(
    *,
    manifest: dict[str, Any],
    benchmarks_root: Path | None,
) -> None:
    compatibility = manifest.get("compatibility")
    if not isinstance(compatibility, dict):
        raise ValueError("Bundle manifest missing compatibility record")

    benchmark_names = compatibility.get("benchmark_names")
    if not isinstance(benchmark_names, list) or not all(
        isinstance(name, str) and name.strip() for name in benchmark_names
    ):
        raise ValueError("Bundle compatibility record is missing benchmark names")

    effective_benchmarks_root = benchmarks_root or Path("benchmarks")
    if benchmarks_root is not None or effective_benchmarks_root.exists():
        missing_benchmarks = sorted(
            benchmark_name
            for benchmark_name in benchmark_names
            if not (effective_benchmarks_root / benchmark_name).exists()
        )
        if missing_benchmarks:
            raise ValueError(
                "Remote benchmarks root "
                f"{effective_benchmarks_root} is missing bundle benchmarks: "
                f"{', '.join(missing_benchmarks)}"
            )

    expected_revision = compatibility.get("source_runtime_revision")
    if isinstance(expected_revision, str) and expected_revision.strip():
        current_revision = _discover_git_revision()
        if current_revision is None:
            raise ValueError(
                "Unable to determine remote runtime revision for compatibility check"
            )
        if current_revision != expected_revision:
            raise ValueError(
                "Bundle runtime revision "
                f"{expected_revision} does not match remote runtime revision "
                f"{current_revision}"
            )


def _write_summary(
    *,
    submission_dir: Path,
    manifest: dict[str, Any] | None,
    state: dict[str, Any],
    exit_code: int,
    error: str | None = None,
) -> None:
    summary = {
        "bundle_id": manifest.get("bundle_id") if manifest is not None else None,
        "source_experiment_name": (
            manifest.get("source_experiment_name")
            if manifest is not None
            else state.get("source_experiment_name")
        ),
        "remote_experiment_name": (
            manifest.get("remote_experiment_name")
            if manifest is not None
            else state.get("remote_experiment_name")
        ),
        "selected_trial_count": (
            manifest.get("selected_trial_count") if manifest is not None else None
        ),
        "skipped_trial_count": (
            manifest.get("skipped_trial_count") if manifest is not None else None
        ),
        "started_at": state.get("started_at"),
        "completed_at": state.get("completed_at"),
        "state": state.get("state"),
        "reeval_exit_code": exit_code,
    }
    if error is not None:
        summary["error"] = error
    (submission_dir / _SUMMARY_FILENAME).write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
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
