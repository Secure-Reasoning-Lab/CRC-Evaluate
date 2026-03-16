"""Per-input Atlantis-backed coverage sessions for timeline analysis."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from crsbench.prepare.uniafl_backend import (
    default_uniafl_root,
    default_uniafl_runtime_image,
)
from crsbench.utils.docker import fix_docker_ownership
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)
RUNTIME_OUT_IGNORES = frozenset({".crsbench-repo"})


CoverageData = dict[str, dict]


@dataclass
class CoverageRunResult:
    """Coverage result for one input."""

    coverage_data: CoverageData
    raw_cov_path: Optional[Path] = None
    raw_artifacts_dir: Optional[Path] = None
    stdout_path: Optional[Path] = None
    stderr_path: Optional[Path] = None
    crashed: bool = False
    crash_log_path: Optional[Path] = None


def _approximate_totals_from_results(
    results: list[CoverageRunResult],
) -> dict[str, float | int]:
    merged_lines: set[tuple[str, int]] = set()
    covered_functions: set[str] = set()
    covered_sources: set[Path] = set()

    for result in results:
        for function_name, data in result.coverage_data.items():
            covered_functions.add(function_name)
            src = data.get("src", "")
            if src:
                src_path = Path(src)
                if src_path.exists():
                    covered_sources.add(src_path)
            for line in data.get("lines", []):
                merged_lines.add((src, int(line)))

    lines_covered = len(merged_lines)
    lines_total = 0
    for src_path in covered_sources:
        try:
            lines_total += len(src_path.read_text().splitlines())
        except OSError:
            continue
    functions_covered = len(covered_functions)
    lines_percent = (lines_covered / lines_total * 100.0) if lines_total > 0 else 0.0
    return {
        "lines_covered": lines_covered,
        "lines_total": lines_total,
        "lines_percent": lines_percent,
        "functions_covered": functions_covered,
        "functions_total": 0,
    }


class CoverageSession(AbstractContextManager["CoverageSession"]):
    """Abstract per-input coverage session."""

    def collect_single(self, corpus_file: Path) -> CoverageRunResult:
        raise NotImplementedError

    def collect_many(self, corpus_files: list[Path]) -> dict[Path, CoverageRunResult]:
        return {path: self.collect_single(path) for path in corpus_files}

    def collect_batch_totals(self, corpus_dir: Path) -> dict:
        del corpus_dir
        raise NotImplementedError

    def close(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class ShardedCoverageSession(CoverageSession):
    """Coverage session wrapper that fans out work across multiple warm sessions."""

    def __init__(self, sessions: list[CoverageSession]):
        if not sessions:
            raise ValueError("ShardedCoverageSession requires at least one session")
        self.sessions = sessions

    def _session_index_for(self, corpus_file: Path) -> int:
        return int(_content_hash(corpus_file), 16) % len(self.sessions)

    def collect_single(self, corpus_file: Path) -> CoverageRunResult:
        return self.sessions[self._session_index_for(corpus_file)].collect_single(
            corpus_file
        )

    def collect_many(self, corpus_files: list[Path]) -> dict[Path, CoverageRunResult]:
        shards: list[list[Path]] = [[] for _ in self.sessions]
        for corpus_file in corpus_files:
            shards[self._session_index_for(corpus_file)].append(corpus_file)

        results: dict[Path, CoverageRunResult] = {}
        with ThreadPoolExecutor(max_workers=len(self.sessions)) as executor:
            futures = [
                executor.submit(session.collect_many, shard)
                for session, shard in zip(self.sessions, shards, strict=False)
                if shard
            ]
            for future in futures:
                results.update(future.result())
        return results

    def collect_batch_totals(self, corpus_dir: Path) -> dict:
        del corpus_dir
        return self._aggregate_shard_totals(
            {
                "lines_covered": 0,
                "lines_total": 0,
                "lines_percent": 0.0,
                "functions_covered": 0,
                "functions_total": 0,
            }
        )

    def _aggregate_shard_totals(self, default_totals: dict) -> dict:
        shard_results: list[CoverageRunResult] = []
        for session in self.sessions:
            collected_results = getattr(session, "_collected_results", None)
            if isinstance(collected_results, dict):
                shard_results.extend(collected_results.values())
        if not shard_results:
            return default_totals
        return _approximate_totals_from_results(shard_results)

    def close(self) -> None:
        for session in reversed(self.sessions):
            session.close()


class UniAFLCoverageSession(CoverageSession):
    """Coverage session for the UniAFL/given_fuzzer coverage backend.

    This session is the single backend contract for both native and JVM
    coverage collection. It encapsulates the given_fuzzer checkout/runtime
    configuration and will own the coverage-only build/prepare/run pipeline.
    """

    def __init__(
        self,
        *,
        project_name: str,
        harness_name: str,
        language: str,
        benchmark_path: Path,
        source_repo_dir: Path,
        build_output_dir: Path,
        output_dir: Path,
        parse_single_output: Callable[[Any], CoverageData],
        parse_textcov_output: Optional[Callable[[Path], CoverageData]],
        parse_summary: Callable[[Path], dict],
        uniafl_root: Optional[Path] = None,
        runtime_image: Optional[str] = None,
        cpu_set: Optional[str] = None,
        session_label: Optional[str] = None,
    ):
        self.project_name = project_name
        self.harness_name = harness_name
        self.language = language.lower()
        self.benchmark_path = Path(benchmark_path).resolve()
        self.source_repo_dir = Path(source_repo_dir).resolve()
        self.build_output_dir = Path(build_output_dir)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir = self.output_dir / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.parse_single_output = parse_single_output
        del parse_textcov_output, parse_summary
        self.uniafl_root = Path(uniafl_root or default_uniafl_root()).resolve()
        self.runtime_image = runtime_image or default_uniafl_runtime_image(
            self.language
        )
        self.cpu_set = cpu_set
        self.session_label = session_label
        self._tempdir = tempfile.TemporaryDirectory(prefix="crsbench-uniafl-session-")
        self.workspace = Path(self._tempdir.name)
        self.runtime_benchmark_dir = self.workspace / "benchmark"
        self.runtime_build_output_dir = self.workspace / "out"
        self.worker_script_path = self.workspace / "crsbench_cov_worker.py"
        worker_log_stem = (
            "worker" if session_label is None else f"worker.{session_label}"
        )
        self.worker_stdout_path = self.raw_dir / f"{worker_log_stem}.stdout.log"
        self.worker_stderr_path = self.raw_dir / f"{worker_log_stem}.stderr.log"
        self.runs_dir = self.workspace / "runs"
        self.container_name = (
            f"crsbench-uniafl-{self.project_name[:20]}-{uuid.uuid4().hex[:10]}"
        )
        self._collected_results: dict[str, CoverageRunResult] = {}
        self._write_worker_script()
        self._prepare_runtime_benchmark()
        self._prepare_runtime_build_output()
        self._start_container()
        self._prepare_harness()

    def _prepare_runtime_benchmark(self) -> None:
        self.runtime_benchmark_dir.mkdir(parents=True, exist_ok=True)
        for child in list(self.runtime_benchmark_dir.iterdir()):
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        shutil.copytree(
            self.benchmark_path.resolve(),
            self.runtime_benchmark_dir,
            dirs_exist_ok=True,
            symlinks=True,
        )

    def _prepare_runtime_build_output(self) -> None:
        fix_docker_ownership(self.build_output_dir)
        self.runtime_build_output_dir.mkdir(parents=True, exist_ok=True)
        for child in list(self.runtime_build_output_dir.iterdir()):
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()

        def _ignore_runtime_entries(_directory: str, names: list[str]) -> list[str]:
            return [
                name
                for name in names
                if name in RUNTIME_OUT_IGNORES or "_corpus" in name
            ]

        shutil.copytree(
            self.build_output_dir.resolve(),
            self.runtime_build_output_dir,
            dirs_exist_ok=True,
            ignore=_ignore_runtime_entries,
            symlinks=True,
        )

    def _resolve_host_source_path(self, src: str) -> str:
        if not src:
            return src
        src_path = Path(src)
        if src_path.is_absolute():
            try:
                if src_path.is_relative_to("/src/repo"):
                    rel = src_path.relative_to("/src/repo")
                    return str(self.source_repo_dir / rel)
                if src_path.is_relative_to("/src"):
                    rel = src_path.relative_to("/src")
                    return str(self.benchmark_path / rel)
            except ValueError:
                return src
        return src

    def _normalize_coverage_data(self, coverage_data: CoverageData) -> CoverageData:
        normalized: CoverageData = {}
        for function_name, data in coverage_data.items():
            item = dict(data)
            src = item.get("src")
            if isinstance(src, str):
                item["src"] = self._resolve_host_source_path(src)
            normalized[function_name] = item
        return normalized

    def _write_worker_script(self) -> None:
        self.worker_script_path.write_text(_UNIAFL_COVERAGE_WORKER_SCRIPT)

    def _start_container(self) -> None:
        cmd = [
            "docker",
            "run",
            "-d",
            "--privileged",
            "--shm-size=2g",
            "--name",
            self.container_name,
            "-e",
            f"CRS_TARGET={self.project_name}",
            "-e",
            "FUZZING_ENGINE=libfuzzer",
            "-e",
            "RUN_FUZZER_MODE=batch",
            "-e",
            "SANITIZER=address",
            "-e",
            "OUT=/out",
            "-e",
            "POV_DIR=/povs",
            "-e",
            "CORPUS_DIR=/corpus",
            "-e",
            "CRS_DATA_DIR=/crs-data",
            "-e",
            "SEED_SHARE_DIR=/shared-seeds",
            "-v",
            f"{self.runtime_benchmark_dir.resolve()}:/src",
            "-v",
            f"{self.source_repo_dir}:/src/repo",
            "-v",
            f"{self.runtime_build_output_dir.resolve()}:/out",
            "-v",
            f"{self.workspace}:/workspace",
        ]
        if self.cpu_set:
            cmd.extend(["--cpuset-cpus", self.cpu_set])
        cmd.extend(
            [
                self.runtime_image,
                "bash",
                "-lc",
                "mkdir -p /povs /corpus /crs-data /shared-seeds && "
                "if [ -d /out/coverage-out ]; then ln -snf /out/coverage-out /coverage-out; fi && "
                'printf \'{"target_harnesses":["%s"]}\\n\' '
                + shlex.quote(self.harness_name)
                + " > /crs.config && sleep infinity",
            ]
        )
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            stderr = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(stderr or "Failed to start UniAFL coverage container")

    def _docker_exec(
        self,
        args: list[str],
        *,
        env: Optional[dict[str, str]] = None,
        timeout: int = 300,
    ) -> subprocess.CompletedProcess[str]:
        cmd = ["docker", "exec"]
        if env:
            for key, value in env.items():
                cmd.extend(["-e", f"{key}={value}"])
        cmd.append(self.container_name)
        cmd.extend(args)
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _prepare_harness(self) -> None:
        result = self._docker_exec(
            [
                "python3",
                "/workspace/crsbench_cov_worker.py",
                "prepare",
                self.harness_name,
            ],
            timeout=300,
        )
        if result.returncode != 0:
            self.worker_stdout_path.write_text(result.stdout or "")
            self.worker_stderr_path.write_text(result.stderr or "")
            stderr = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(
                stderr or f"Failed to prepare UniAFL coverage for {self.harness_name}"
            )

    def _workspace_container_path(self, host_path: Path) -> str:
        relative = host_path.resolve().relative_to(self.workspace.resolve())
        return str(Path("/workspace") / relative)

    def _append_worker_logs(self, stdout: str, stderr: str) -> None:
        if stdout:
            with self.worker_stdout_path.open("a") as handle:
                handle.write(stdout)
        if stderr:
            with self.worker_stderr_path.open("a") as handle:
                handle.write(stderr)

    def _per_input_timeout_seconds(self) -> int:
        return 300 if self.language == "jvm" else 5

    def _batch_timeout_seconds(self, input_count: int) -> int:
        return max(
            600 if self.language == "jvm" else 120,
            self._per_input_timeout_seconds() * max(1, input_count) + 120,
        )

    def _load_result_from_output_root(
        self,
        *,
        corpus_hash: str,
        output_root: Path,
    ) -> CoverageRunResult:
        status_path = output_root / f"{corpus_hash}.status.json"
        cov_path = output_root / f"{corpus_hash}.cov"
        if not status_path.exists():
            raise RuntimeError(
                f"UniAFL coverage output missing status for {corpus_hash}"
            )

        raw_cov_path = self.raw_dir / f"{corpus_hash}.cov"
        if cov_path.exists():
            shutil.copy2(cov_path, raw_cov_path)
            coverage_data = self._normalize_coverage_data(
                json.loads(raw_cov_path.read_text())
            )
        else:
            coverage_data = {}
            raw_cov_path.write_text("{}")
        raw_cov_path.write_text(json.dumps(coverage_data))

        status = json.loads(status_path.read_text())
        stdout_path = None
        stderr_path = None
        crash_log_path = None
        for suffix in ("stdout.log", "stderr.log", "crash.log"):
            candidate = output_root / f"{corpus_hash}.{suffix}"
            if candidate.exists():
                dest = self.raw_dir / f"{corpus_hash}.{suffix}"
                shutil.copy2(candidate, dest)
                if suffix == "stdout.log":
                    stdout_path = dest
                elif suffix == "stderr.log":
                    stderr_path = dest
                else:
                    crash_log_path = dest

        return CoverageRunResult(
            coverage_data=coverage_data,
            raw_cov_path=raw_cov_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            crashed=bool(status.get("crashed", False)),
            crash_log_path=crash_log_path,
        )

    def _run_dir_batch(self, corpus_files: list[Path]) -> None:
        if not corpus_files:
            return

        run_root = self.runs_dir / uuid.uuid4().hex
        seed_root = run_root / "seeds"
        output_root = run_root / "outputs"
        seed_root.mkdir(parents=True, exist_ok=True)
        output_root.mkdir(parents=True, exist_ok=True)

        unique_hashes: dict[str, Path] = {}
        for corpus_file in corpus_files:
            corpus_hash = _content_hash(corpus_file)
            if corpus_hash in unique_hashes or corpus_hash in self._collected_results:
                continue
            unique_hashes[corpus_hash] = corpus_file
            shutil.copy2(corpus_file, seed_root / corpus_hash)

        if not unique_hashes:
            return

        container_seed_root = self._workspace_container_path(seed_root)
        container_output_root = self._workspace_container_path(output_root)
        result = self._docker_exec(
            [
                "python3",
                "/workspace/crsbench_cov_worker.py",
                "run-dir",
                self.harness_name,
                container_seed_root,
                container_output_root,
            ],
            env={"CRSBENCH_PER_INPUT_TIMEOUT": str(self._per_input_timeout_seconds())},
            timeout=self._batch_timeout_seconds(len(unique_hashes)),
        )
        self._append_worker_logs(result.stdout or "", result.stderr or "")
        if result.returncode != 0:
            stderr = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(
                stderr
                or f"Failed to run coverage shard for {self.project_name}/{self.harness_name}"
            )
        self._docker_exec(
            [
                "bash",
                "-lc",
                (
                    f"chown -R {os.getuid()}:{os.getgid()} "
                    f"{shlex.quote(container_output_root)} || true"
                ),
            ],
            timeout=60,
        )

        for corpus_hash in unique_hashes:
            self._collected_results[corpus_hash] = self._load_result_from_output_root(
                corpus_hash=corpus_hash,
                output_root=output_root,
            )

    def collect_single(self, corpus_file: Path) -> CoverageRunResult:
        return self.collect_many([corpus_file])[corpus_file]

    def collect_many(self, corpus_files: list[Path]) -> dict[Path, CoverageRunResult]:
        self._run_dir_batch(corpus_files)
        return {
            corpus_file: self._collected_results[_content_hash(corpus_file)]
            for corpus_file in corpus_files
        }

    def collect_batch_totals(self, corpus_dir: Path) -> dict:
        del corpus_dir
        return _approximate_totals_from_results(list(self._collected_results.values()))

    def close(self) -> None:
        subprocess.run(
            ["docker", "rm", "-f", self.container_name],
            capture_output=True,
            text=True,
        )
        self._tempdir.cleanup()


def _content_hash(path: Path) -> str:
    sha256 = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(8192):
            sha256.update(chunk)
    return sha256.hexdigest()[:16]


_UNIAFL_COVERAGE_WORKER_SCRIPT = r"""#!/usr/bin/env python3
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import yaml
from libCRS import Config, init_cp_in_runner, util
from libCRS.ossfuzz_lib import get_harness_names


def _get_run_fuzzer_opt(harness_name: str) -> tuple[int, bool]:
    opt_file = Path("/out") / f"{harness_name}.options"
    if opt_file.exists():
        lines = [
            line
            for line in opt_file.read_text().splitlines()
            if "close_fd_mask" not in line
        ]
        opt_file.write_text("\n".join(lines) + ("\n" if lines else ""))

    env = os.environ.copy()
    env["SKIP_SEED_CORPUS"] = "1"
    result = subprocess.run(
        ["get_run_fuzzer_opt", harness_name],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    opts = shlex.split(result.stdout)[1:]
    max_len = 4096
    allow_timeout_bug = True
    for opt in opts:
        if opt.startswith("-max_len"):
            try:
                max_len = min(int(opt.split("=")[-1]), 1024 * 1024)
            except ValueError:
                pass
        if opt == "-timeout_exitcode=0":
            allow_timeout_bug = False
    return max_len, allow_timeout_bug


def _measure_ms_per_exec(harness_name: str, workdir: Path) -> int:
    tmp = workdir / "crsbench_tmp_seed"
    tmp.write_text("A")
    env = os.environ.copy()
    env["TESTCASE"] = f"{tmp} {tmp}"
    key = f"Executed {tmp} in"
    for _ in range(5):
        result = subprocess.run(
            ["reproduce", harness_name, "-timeout=100"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        stderr = result.stderr or ""
        if key not in stderr:
            continue
        try:
            return int(stderr.split(key, 1)[1].split(" ms", 1)[0].strip())
        except (IndexError, ValueError):
            continue
    return 0


def _prepare(harness_name: str) -> int:
    Config(0, 1).load("/crs.config")
    config_dir = Path("/src/.aixcc")
    config_dir.mkdir(parents=True, exist_ok=True)
    generated_config = config_dir / "config.yaml"
    if not generated_config.exists():
        env = os.environ.copy()
        env["CREATE_CONF"] = str(generated_config)
        subprocess.run(
            ["python3", "/usr/local/bin/main.py"],
            env=env,
            check=True,
        )
    cp = init_cp_in_runner()
    if generated_config.exists():
        generated = yaml.safe_load(generated_config.read_text()) or {}
        configured_harnesses = {
            item.get("name")
            for item in generated.get("harness_files", [])
            if isinstance(item, dict)
        }
        available_harnesses = set(get_harness_names(Path("/out")))
        if harness_name in available_harnesses and harness_name not in configured_harnesses:
            generated_config.unlink()
            cp = init_cp_in_runner()
    harness = cp.get_harnesses()[harness_name]

    workdir = Path(f"/executor/{harness.name}")
    dummy_dir = workdir / "dummy"
    for name in ["uniafl_corpus", "uniafl_cov", "others_corpus", "pov", "workdir"]:
        (dummy_dir / name).mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["bash", "-lc", f"chmod -R a+w '{workdir}'; chmod -R +t '{workdir}'"],
        check=False,
    )

    port = int(os.environ.get("CRSBENCH_REDIS_PORT", "22333"))
    subprocess.run(
        [
            "redis-server",
            "--port",
            str(port),
            "--bind",
            "localhost",
            "--daemonize",
            "yes",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    redis_url = f"localhost:{port}"
    max_len, allow_timeout_bug = _get_run_fuzzer_opt(harness.name)
    ms_per_exec = 0 if cp.language == "jvm" else _measure_ms_per_exec(harness.name, workdir)
    config = {
        "project_src_dir": str(cp.cp_src_path),
        "harness_src_path": str(harness.src_path),
        "given_fuzzer_dir": str(cp.built_path),
        "corpus_dir": str(dummy_dir / "uniafl_corpus"),
        "cov_dir": str(dummy_dir / "uniafl_cov"),
        "given_corpus_dir": str(dummy_dir / "others_corpus"),
        "pov_dir": os.environ.get("POV_DIR", "/povs"),
        "workdir": str(dummy_dir / "workdir"),
        "language": cp.language,
        "redis_url": redis_url,
        "ms_per_exec": ms_per_exec,
        "max_len": max_len,
        "allow_timeout_bug": allow_timeout_bug,
        "harness_name": f"{harness.name}_executor_0",
        "harness_path": str(harness.bin_path),
        "core_ids": [0],
    }
    config_path = workdir / "config_0"
    config_path.write_text(json.dumps(config))

    env = os.environ.copy()
    if cp.language == "jvm":
        env["JAZZER_MAX_NUM_COUNTERS"] = str(128 << 20)
        subprocess.run(
            [
                "run_fuzzer",
                harness.name,
                "--uniafl_coverage",
                "--uniafl_prepare",
                f"--redis_url={redis_url}",
            ],
            env=env,
            check=True,
        )
    else:
        subprocess.run(
            [
                "cfg_analyzer.py",
                "--harness",
                str(harness.bin_path),
                "--redis_url",
                redis_url,
                "--ncpu",
                "1",
            ],
            env=env,
            check=True,
        )
    return 0


def _per_input_timeout_seconds() -> int:
    try:
        return max(1, int(os.environ.get("CRSBENCH_PER_INPUT_TIMEOUT", "60")))
    except ValueError:
        return 60


def _timeout_handler(_signum, _frame):
    raise TimeoutError("coverage input timed out")


def _reset_harness_runner(harness) -> None:
    runner = getattr(harness, "runner", None)
    if runner is None:
        return
    try:
        runner.kill()
    except ProcessLookupError:
        pass
    except Exception:
        pass
    harness.runner = None


def _process_one_input(harness_name: str, harness, blob_path: Path, output_root: Path) -> None:
    worker_idx = int(os.environ.get("CUR_WORKER", "0"))
    raw_cov = Path(f"/executor/{harness_name}/dummy/uniafl_cov/tmp_{worker_idx}")
    line_cov = Path(str(raw_cov) + ".cov")
    for candidate in (raw_cov, line_cov):
        if candidate.exists():
            candidate.unlink()

    timeout_seconds = _per_input_timeout_seconds()
    previous_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(timeout_seconds)
    timed_out = False
    try:
        stdout, stderr, _coverage, crash_log = harness.run_input(str(blob_path))
    except TimeoutError:
        timed_out = True
        _reset_harness_runner(harness)
        stdout = b""
        stderr = f"Timed out after {timeout_seconds}s while processing {blob_path.name}\n".encode()
        crash_log = stderr
        _coverage = b""
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)

    seed_name = blob_path.name
    if raw_cov.exists():
        shutil.move(str(raw_cov), str(output_root / f"{seed_name}.raw_cov"))
    if line_cov.exists():
        shutil.move(str(line_cov), str(output_root / f"{seed_name}.cov"))
    else:
        (output_root / f"{seed_name}.cov").write_text("{}")
    (output_root / f"{seed_name}.stdout.log").write_bytes(stdout or b"")
    (output_root / f"{seed_name}.stderr.log").write_bytes(stderr or b"")
    if crash_log:
        (output_root / f"{seed_name}.crash.log").write_bytes(crash_log)
    (output_root / f"{seed_name}.status.json").write_text(
        json.dumps(
            {
                "crashed": bool(crash_log),
                "timed_out": timed_out,
                "coverage_bytes": len(_coverage or b""),
            }
        )
    )


def _run_inputs(harness_name: str, output_dir: str, inputs: list[str]) -> int:
    Config(0, 1).load("/crs.config")
    cp = init_cp_in_runner()
    harness = cp.get_harnesses()[harness_name]
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    for blob_path in inputs:
        _process_one_input(harness_name, harness, Path(blob_path), output_root)
    return 0


def _run_dir(harness_name: str, seed_dir: str, output_dir: str) -> int:
    seed_root = Path(seed_dir)
    if not seed_root.exists():
        raise FileNotFoundError(f"seed directory not found: {seed_root}")
    inputs = [
        str(path)
        for path in sorted(seed_root.iterdir())
        if path.is_file() and not path.name.startswith(".")
    ]
    return _run_inputs(harness_name, output_dir, inputs)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit("usage: crsbench_cov_worker.py prepare|run-dir <harness> ...")
    command = sys.argv[1]
    harness = sys.argv[2]
    if command == "prepare":
        raise SystemExit(_prepare(harness))
    if command == "run-dir":
        raise SystemExit(_run_dir(harness, sys.argv[3], sys.argv[4]))
    raise SystemExit(f"unknown command: {command}")
"""
