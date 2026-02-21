#!/usr/bin/env python3
"""Manifest-driven smoke runner for CRS regression checks."""

from __future__ import annotations

import argparse
import json
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from crsbench.utils.litellm_env import (
    required_env_errors_for_mode,
    resolve_litellm_runtime_env,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "ci-tests" / "smoke-manifest.yaml"


def run(cmd: list[str], *, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, text=True, check=check, env=env)


def best_effort_cleanup(path: Path) -> None:
    if not path.exists():
        return

    try:
        shutil.rmtree(path)
        return
    except Exception:
        pass

    # Docker fallback for root-owned artifacts created during runs.
    try:
        run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{path}:/cleanup-path",
                "alpine",
                "sh",
                "-lc",
                "rm -rf /cleanup-path/* /cleanup-path/.[!.]* /cleanup-path/..?*",
            ],
            check=False,
        )
    except Exception:
        pass

    try:
        shutil.rmtree(path)
    except Exception:
        pass


def load_manifest(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict) or "suites" not in data:
        raise ValueError(f"Invalid manifest: {path}")
    return data


def build_experiment_config(
    suite_name: str,
    suite: dict[str, Any],
    defaults: dict[str, Any],
    *,
    experiment_filestore: Path,
    report_filestore: Path,
) -> dict[str, Any]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    prefix = suite.get("experiment_prefix", f"smoke-{suite_name}")
    experiment_name = f"{prefix}-{stamp}"

    timeouts = suite["timeouts"]

    config: dict[str, Any] = {
        "experiment": experiment_name,
        "trials": int(suite.get("trials", defaults.get("trials", 1))),
        "mode": suite.get("mode", defaults.get("mode", "delta")),
        "adapter": suite.get("adapter", defaults.get("adapter", "oss-crs")),
        "max_total_time": int(timeouts["max_total_time"]),
        "build_timeout": int(timeouts["build_timeout"]),
        "run_timeout": int(timeouts["run_timeout"]),
        "verify_timeout": int(timeouts["verify_timeout"]),
        "difficulty_level": int(suite.get("difficulty_level", defaults.get("difficulty_level", 1))),
        "experiment_filestore": str(experiment_filestore),
        "report_filestore": str(report_filestore),
        "crses": [suite["crs"]],
        "redis_host": suite.get("redis_host", defaults.get("redis_host", "localhost")),
        "resources": suite["resources"],
        "worker": {
            "jobs": int(suite.get("worker_jobs", defaults.get("worker_jobs", 3))),
            "cleanup_after_trial": bool(
                suite.get("cleanup_after_trial", defaults.get("cleanup_after_trial", False))
            ),
        },
    }

    if "benchmark_suite" in suite or "benchmark_suite" in defaults:
        config["benchmark_suite"] = suite.get("benchmark_suite", defaults.get("benchmark_suite"))
    if "benchmarks" in suite:
        config["benchmarks"] = suite["benchmarks"]

    if "skip_litellm" in suite:
        config["skip_litellm"] = bool(suite["skip_litellm"])
    if "litellm_mode" in suite:
        config["litellm_mode"] = suite["litellm_mode"]
    if "llm_tracking_enabled" in suite:
        config["llm_tracking_enabled"] = bool(suite["llm_tracking_enabled"])

    if "pov_early_stop" in suite:
        config["pov_early_stop"] = bool(suite["pov_early_stop"])

    if "per_pov_verify_timeout" in timeouts:
        config["per_pov_verify_timeout"] = int(timeouts["per_pov_verify_timeout"])

    if "max_pov_variants_per_cpv" in suite:
        config["max_pov_variants_per_cpv"] = int(suite["max_pov_variants_per_cpv"])

    if "crs_compose" in suite:
        config["crs_compose"] = suite["crs_compose"]

    return config


def wait_for_worker_start(proc: subprocess.Popen[str], timeout_s: float = 12.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("Worker exited before orchestrator started")
        time.sleep(0.4)


def terminate_worker(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def summarize_trial_status(exp_dir: Path) -> tuple[int, int, int, list[Path]]:
    trial_dirs = sorted(p for p in exp_dir.rglob("trial-*") if p.is_dir())
    successes = sum((t / ".success").exists() for t in trial_dirs)
    failures = sum((t / ".failed").exists() for t in trial_dirs)
    patch_files = sum(len(list((t / "output" / "patches").glob("*"))) for t in trial_dirs if (t / "output" / "patches").exists())
    return successes, failures, patch_files, trial_dirs


def validate_bugfinding_cpvs(exp_dir: Path, expected_cpvs: dict[str, str]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    stores = list(exp_dir.rglob("pov_store.json"))
    by_key: dict[str, Path] = {}
    for store in stores:
        # .../<crs>/<benchmark>/<harness>/delta/address/trial-X/povs/pov_store.json
        try:
            harness = store.parents[4].name
            benchmark = store.parents[5].name
            by_key[f"{benchmark}/{harness}"] = store
        except Exception:
            continue

    for key, expected in expected_cpvs.items():
        store = by_key.get(key)
        if store is None:
            errors.append(f"missing pov_store for {key}")
            continue
        data = json.loads(store.read_text())
        matched: set[str] = set()
        for pov in data.get("povs", {}).values():
            for cpv in pov.get("cpv_matched", []):
                matched.add(cpv)
        if expected not in matched:
            errors.append(f"{key}: expected {expected}, got {sorted(matched) if matched else 'none'}")

    return len(errors) == 0, errors


def run_suite(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    defaults = manifest.get("defaults", {})
    suites = manifest["suites"]
    if args.suite not in suites:
        print(f"Unknown suite: {args.suite}. Available: {', '.join(sorted(suites))}", file=sys.stderr)
        return 2

    suite = suites[args.suite]
    litellm_mode = suite.get("litellm_mode")
    if litellm_mode:
        runtime_env = resolve_litellm_runtime_env(litellm_mode)
        runtime_errors = required_env_errors_for_mode(
            runtime_env,
            tracking_enabled=bool(suite.get("llm_tracking_enabled", True)),
        )
        if runtime_errors:
            print(
                "[smoke] missing LiteLLM runtime env for suite "
                f"'{args.suite}' ({litellm_mode}): {'; '.join(runtime_errors)}",
                file=sys.stderr,
            )
            return 2

    root = Path(args.result_root).resolve()
    root.mkdir(parents=True, exist_ok=True)

    workspace = Path(tempfile.mkdtemp(prefix=f"crsbench-smoke-{args.suite}-", dir=str(root)))
    exp_filestore = workspace / "experiment-data"
    report_filestore = workspace / "report-data"
    exp_filestore.mkdir(parents=True, exist_ok=True)
    report_filestore.mkdir(parents=True, exist_ok=True)

    config = build_experiment_config(
        args.suite,
        suite,
        defaults,
        experiment_filestore=exp_filestore,
        report_filestore=report_filestore,
    )
    config_path = workspace / f"{args.suite}-config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))

    print(f"[smoke] suite={args.suite}")
    print(f"[smoke] config={config_path}")
    print(f"[smoke] workspace={workspace}")

    if args.clean_valkey:
        run(["/usr/bin/env", "bash", "-lc", "printf 'yes\\n' | uv run python scripts/valkey-helper.py clean-all"])

    run(["uv", "run", "crsbench", "run", "--experiment-config", str(config_path), "--dry-run"])

    worker_cmd = [
        "uv",
        "run",
        "crsbench",
        "worker",
        "--experiment-config",
        str(config_path),
    ]
    if args.worker_cpuset:
        worker_cmd += ["--cores", args.worker_cpuset]
    else:
        worker_cmd += ["--cores", str(args.worker_cores)]

    worker_log = workspace / "worker-supervisor.log"
    with worker_log.open("w") as wf:
        worker = subprocess.Popen(worker_cmd, stdout=wf, stderr=subprocess.STDOUT, text=True)

    try:
        wait_for_worker_start(worker)
        run(["uv", "run", "crsbench", "run", "--experiment-config", str(config_path)])
    finally:
        terminate_worker(worker)

    exp_dir = exp_filestore / config["experiment"]
    successes, failures, patch_files, trial_dirs = summarize_trial_status(exp_dir)

    errors: list[str] = []
    if failures > 0:
        errors.append(f"{failures} trial(s) failed")
    if successes != len(trial_dirs):
        errors.append(f"expected all trials successful, got success={successes}, total={len(trial_dirs)}")

    min_patch = suite.get("min_patch_files_per_trial")
    if min_patch is not None:
        for trial_dir in trial_dirs:
            n = 0
            patches_dir = trial_dir / "output" / "patches"
            if patches_dir.exists():
                n = len([p for p in patches_dir.iterdir() if p.is_file()])
            if n < int(min_patch):
                errors.append(f"{trial_dir}: patch_files={n} < {min_patch}")

    if args.suite == "bugfinding" and suite.get("expected_cpvs"):
        ok, cpv_errors = validate_bugfinding_cpvs(exp_dir, suite["expected_cpvs"])
        if not ok:
            errors.extend(cpv_errors)

    summary = {
        "suite": args.suite,
        "experiment": config["experiment"],
        "workspace": str(workspace),
        "successes": successes,
        "failures": failures,
        "total_trials": len(trial_dirs),
        "patch_files": patch_files,
        "errors": errors,
        "status": "pass" if not errors else "fail",
    }

    summary_path = workspace / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))

    if args.keep_workspace:
        print(f"[smoke] preserved workspace: {workspace}")
    else:
        best_effort_cleanup(workspace)

    return 0 if not errors else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run CRS smoke-check suite")
    parser.add_argument("--suite", required=True, help="Suite name from smoke-manifest.yaml")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--worker-cores", type=int, default=16)
    parser.add_argument(
        "--worker-cpuset",
        default="",
        help="Optional cpuset for worker cores (e.g. 0-15). Overrides --worker-cores.",
    )
    parser.add_argument(
        "--result-root",
        default="/tmp/crsbench-smoke",
        help="Base directory for generated configs/logs/results",
    )
    parser.add_argument("--clean-valkey", action="store_true", help="Flush Valkey before run")
    parser.add_argument("--keep-workspace", action="store_true", help="Keep workspace after run")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return run_suite(args)


if __name__ == "__main__":
    raise SystemExit(main())
