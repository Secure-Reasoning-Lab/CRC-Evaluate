#!/usr/bin/env python3
"""Manifest-driven smoke runner for CRS regression checks."""

from __future__ import annotations

import argparse
import json
import os
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
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = ROOT / "scripts" / "ci-tests" / "smoke-manifest.yaml"

# Keep local smoke behavior consistent with CRSBench CLI entrypoints.
load_dotenv()


def run(
    cmd: list[str], *, check: bool = True, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
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

    redis_host = os.environ.get("CRSBENCH_REDIS_HOST") or suite.get(
        "redis_host", defaults.get("redis_host", "localhost")
    )

    config: dict[str, Any] = {
        "experiment": experiment_name,
        "trials": int(suite.get("trials", defaults.get("trials", 1))),
        "mode": suite.get("mode", defaults.get("mode", "delta")),
        "adapter": suite.get("adapter", defaults.get("adapter", "oss-crs")),
        "max_total_time": int(timeouts["max_total_time"]),
        "build_timeout": int(timeouts["build_timeout"]),
        "run_timeout": int(timeouts["run_timeout"]),
        "verify_timeout": int(timeouts["verify_timeout"]),
        "difficulty_level": int(
            suite.get("difficulty_level", defaults.get("difficulty_level", 1))
        ),
        "experiment_filestore": str(experiment_filestore),
        "report_filestore": str(report_filestore),
        "crses": [suite["crs"]],
        "redis_host": redis_host,
        "resources": suite["resources"],
        "worker": {
            "jobs": int(suite.get("worker_jobs", defaults.get("worker_jobs", 3))),
            "cleanup_after_trial": bool(
                suite.get(
                    "cleanup_after_trial", defaults.get("cleanup_after_trial", False)
                )
            ),
        },
    }

    if "benchmark_suite" in suite or "benchmark_suite" in defaults:
        config["benchmark_suite"] = suite.get(
            "benchmark_suite", defaults.get("benchmark_suite")
        )
    if "benchmarks" in suite:
        config["benchmarks"] = suite["benchmarks"]

    if "skip_litellm" in suite:
        config["skip_litellm"] = bool(suite["skip_litellm"])
    if "skip_verification" in suite:
        config["skip_verification"] = bool(suite["skip_verification"])
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
    if "crs_overrides" in suite:
        config["crs_overrides"] = suite["crs_overrides"]

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


def _tail_text(path: Path, lines: int = 80) -> str:
    if not path.exists():
        return "(log file missing)"
    text = path.read_text(errors="replace")
    data = text.splitlines()
    if len(data) <= lines:
        return text
    return "\n".join(data[-lines:])


def _count_files(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for p in path.iterdir() if p.is_file())


def _sum_llm_usage(exp_dir: Path) -> tuple[float, int, int]:
    """Aggregate LLM usage from per-trial llm-usage.json files.

    Returns:
        (total_cost_usd, total_requests, usage_file_count)
    """
    total_cost = 0.0
    total_requests = 0
    usage_files = 0
    for usage_path in exp_dir.rglob("llm-usage.json"):
        try:
            data = json.loads(usage_path.read_text())
        except Exception:
            continue
        usage_files += 1
        total_cost += float(data.get("total_cost_usd", 0.0) or 0.0)
        # New schema: request_count / total_api_calls. Keep legacy fallback.
        if "request_count" in data:
            total_requests += int(data.get("request_count", 0) or 0)
        elif "total_api_calls" in data:
            total_requests += int(data.get("total_api_calls", 0) or 0)
        else:
            total_requests += int(data.get("num_requests", 0) or 0)
    return total_cost, total_requests, usage_files


def _compose_has_llm_config(compose_path: Path) -> bool:
    """Return True when compose YAML includes llm_config section."""
    try:
        data = yaml.safe_load(compose_path.read_text()) or {}
    except Exception:
        return False
    return isinstance(data, dict) and "llm_config" in data


def summarize_trial_status(exp_dir: Path) -> tuple[int, int, int, int, list[Path]]:
    trial_dirs = sorted(p for p in exp_dir.rglob("trial-*") if p.is_dir())
    successes = 0
    failures = 0
    for trial_dir in trial_dirs:
        if (trial_dir / ".success").exists():
            successes += 1
            continue
        # Current marker name is .fail; keep .failed as legacy fallback.
        if (trial_dir / ".fail").exists() or (trial_dir / ".failed").exists():
            failures += 1
            continue
        # Fallback to orchestrator metadata if marker writes were interrupted.
        metadata_path = trial_dir / "metadata.json"
        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text())
                if metadata.get("success") is True:
                    successes += 1
                elif metadata.get("success") is False:
                    failures += 1
            except Exception:
                pass
    patch_files = sum(_count_files(t / "output" / "patches") for t in trial_dirs)
    pov_files = sum(_count_files(t / "output" / "povs") for t in trial_dirs)
    return successes, failures, patch_files, pov_files, trial_dirs


def validate_bugfinding_cpvs(
    exp_dir: Path, expected_cpvs: dict[str, str]
) -> tuple[bool, list[str]]:
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
            errors.append(
                f"{key}: expected {expected}, got {sorted(matched) if matched else 'none'}"
            )

    return len(errors) == 0, errors


def run_suite(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    defaults = manifest.get("defaults", {})
    suites = manifest["suites"]
    if args.suite not in suites:
        print(
            f"Unknown suite: {args.suite}. Available: {', '.join(sorted(suites))}",
            file=sys.stderr,
        )
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

    workspace = Path(
        tempfile.mkdtemp(prefix=f"crsbench-smoke-{args.suite}-", dir=str(root))
    )
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
        run(
            [
                "/usr/bin/env",
                "bash",
                "-lc",
                "printf 'yes\\n' | uv run python scripts/valkey-helper.py clean-all",
            ]
        )

    run(
        [
            "uv",
            "run",
            "crsbench",
            "run",
            "--experiment-config",
            str(config_path),
            "--dry-run",
        ]
    )

    worker_cmd = [
        "uv",
        "run",
        "crsbench",
        "worker",
        "--experiment-config",
        str(config_path),
        "--continuous",
    ]
    if args.no_cpuset:
        worker_cmd.append("--no-cpuset")
    if args.worker_cpuset:
        worker_cmd += ["--cores", args.worker_cpuset]
    else:
        worker_cmd += ["--cores", str(args.worker_cores)]

    orchestrator_cmd = [
        "uv",
        "run",
        "crsbench",
        "run",
        "--experiment-config",
        str(config_path),
    ]
    orchestrator_log = workspace / "orchestrator.log"
    with orchestrator_log.open("w") as of:
        orchestrator = subprocess.Popen(
            orchestrator_cmd, stdout=of, stderr=subprocess.STDOUT, text=True
        )

    worker_log = workspace / "worker-supervisor.log"
    with worker_log.open("w") as wf:
        worker = subprocess.Popen(
            worker_cmd, stdout=wf, stderr=subprocess.STDOUT, text=True
        )

    orchestrator_rc: int | None = None
    try:
        try:
            wait_for_worker_start(worker)
        except RuntimeError as first_start_err:
            # In CI/cpuset mode, worker can occasionally exit during startup.
            # Retry once while orchestrator is already running.
            if orchestrator.poll() is None:
                with worker_log.open("a") as wf:
                    worker = subprocess.Popen(
                        worker_cmd, stdout=wf, stderr=subprocess.STDOUT, text=True
                    )
                try:
                    wait_for_worker_start(worker, timeout_s=8.0)
                except RuntimeError as second_start_err:
                    raise RuntimeError(
                        "Worker failed to stay alive during startup after retry.\n"
                        f"First error: {first_start_err}\n"
                        f"Second error: {second_start_err}\n"
                        "=== worker-supervisor.log (tail) ===\n"
                        f"{_tail_text(worker_log)}\n"
                        "=== orchestrator.log (tail) ===\n"
                        f"{_tail_text(orchestrator_log)}"
                    ) from second_start_err
            else:
                raise RuntimeError(
                    "Worker exited during startup and orchestrator was already down.\n"
                    f"Error: {first_start_err}\n"
                    "=== worker-supervisor.log (tail) ===\n"
                    f"{_tail_text(worker_log)}\n"
                    "=== orchestrator.log (tail) ===\n"
                    f"{_tail_text(orchestrator_log)}"
                ) from first_start_err
        orchestrator_rc = orchestrator.wait()
    finally:
        terminate_worker(worker)
        if orchestrator.poll() is None:
            orchestrator.terminate()
            try:
                orchestrator.wait(timeout=10)
            except subprocess.TimeoutExpired:
                orchestrator.kill()

    if orchestrator_rc != 0:
        raise RuntimeError(
            "Orchestrator exited with non-zero status "
            f"({orchestrator_rc}).\n"
            "=== orchestrator.log (tail) ===\n"
            f"{_tail_text(orchestrator_log)}\n"
            "=== worker-supervisor.log (tail) ===\n"
            f"{_tail_text(worker_log)}"
        )

    exp_dir = exp_filestore / config["experiment"]
    successes, failures, patch_files, pov_files, trial_dirs = summarize_trial_status(
        exp_dir
    )
    llm_total_cost, llm_total_requests, llm_usage_files = _sum_llm_usage(exp_dir)

    errors: list[str] = []
    if failures > 0:
        errors.append(f"{failures} trial(s) failed")
    if successes != len(trial_dirs):
        errors.append(
            f"expected all trials successful, got success={successes}, total={len(trial_dirs)}"
        )

    min_patch = suite.get("min_patch_files_per_trial")
    if min_patch is not None:
        for trial_dir in trial_dirs:
            n = _count_files(trial_dir / "output" / "patches")
            if n < int(min_patch):
                errors.append(f"{trial_dir}: patch_files={n} < {min_patch}")

    min_patch_total = suite.get("min_patch_files_total")
    if min_patch_total is not None and patch_files < int(min_patch_total):
        errors.append(f"patch_files_total={patch_files} < {min_patch_total}")

    min_pov = suite.get("min_pov_files_per_trial")
    if min_pov is not None:
        for trial_dir in trial_dirs:
            n = _count_files(trial_dir / "output" / "povs")
            if n < int(min_pov):
                errors.append(f"{trial_dir}: pov_files={n} < {min_pov}")

    if args.suite == "bugfinding" and suite.get("expected_cpvs"):
        ok, cpv_errors = validate_bugfinding_cpvs(exp_dir, suite["expected_cpvs"])
        if not ok:
            errors.extend(cpv_errors)

    if suite.get("assert_no_oss_crs_litellm"):
        for trial_dir in trial_dirs:
            compose_path = trial_dir / "crs-compose.yaml"
            if compose_path.exists() and _compose_has_llm_config(compose_path):
                errors.append(
                    f"{compose_path}: expected no llm_config when skip_litellm=true"
                )

    min_llm_requests = suite.get("min_llm_total_requests")
    if min_llm_requests is not None and llm_total_requests < int(min_llm_requests):
        errors.append(
            f"llm_total_requests={llm_total_requests} < {int(min_llm_requests)}"
        )

    min_llm_cost = suite.get("min_llm_total_cost_usd")
    if min_llm_cost is not None and llm_total_cost < float(min_llm_cost):
        errors.append(f"llm_total_cost_usd={llm_total_cost:.6f} < {float(min_llm_cost)}")

    summary = {
        "suite": args.suite,
        "experiment": config["experiment"],
        "workspace": str(workspace),
        "successes": successes,
        "failures": failures,
        "total_trials": len(trial_dirs),
        "patch_files": patch_files,
        "pov_files": pov_files,
        "llm_total_cost_usd": llm_total_cost,
        "llm_total_requests": llm_total_requests,
        "llm_usage_files": llm_usage_files,
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
    parser.add_argument(
        "--suite", required=True, help="Suite name from smoke-manifest.yaml"
    )
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--worker-cores", type=int, default=16)
    parser.add_argument(
        "--worker-cpuset",
        default="",
        help="Optional cpuset for worker cores (e.g. 0-15). Overrides --worker-cores.",
    )
    parser.add_argument(
        "--no-cpuset",
        action="store_true",
        help="Run worker without cpuset/cgroup pinning (portable across Docker cgroup drivers).",
    )
    parser.add_argument(
        "--result-root",
        default="/tmp/crsbench-smoke",
        help="Base directory for generated configs/logs/results",
    )
    parser.add_argument(
        "--clean-valkey", action="store_true", help="Flush Valkey before run"
    )
    parser.add_argument(
        "--keep-workspace", action="store_true", help="Keep workspace after run"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return run_suite(args)


if __name__ == "__main__":
    raise SystemExit(main())
