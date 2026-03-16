"""Per-input coverage backend for timeline analysis.

This module provides a warm Docker-backed coverage session used by the
timeline-oriented coverage flows. The session keeps one runtime container
alive per ``(project, harness)`` and executes the official base-runner
``coverage`` script repeatedly against staged seed sets, persisting raw
artifacts per input.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from crsbench.evaluation.process_utils import run_with_graceful_timeout
from crsbench.prepare.uniafl_backend import (
    default_uniafl_builder_image,
    default_uniafl_clang_image,
    default_uniafl_root,
    default_uniafl_runtime_image,
)
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)
BASE_RUNNER_IMAGE = "gcr.io/oss-fuzz-base/base-runner"
NATIVE_COVERAGE_LANGUAGES = {"c", "cpp", "c++", "rust", "go"}
WARM_WORKER_TIMEOUT_SECONDS = 300


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


class SingleShotCoverageSession(CoverageSession):
    """Fallback session that delegates to the existing strategy methods."""

    def __init__(
        self,
        *,
        harness_name: str,
        output_dir: Path,
        collect_single_fn: Callable[[str, Path], CoverageData],
        collect_batch_fn: Callable[[Path, Path], Path],
        parse_summary_fn: Callable[[Path], dict],
    ):
        self.harness_name = harness_name
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._raw_dir = self.output_dir / "raw"
        self._raw_dir.mkdir(parents=True, exist_ok=True)
        self._collect_single_fn = collect_single_fn
        self._collect_batch_fn = collect_batch_fn
        self._parse_summary_fn = parse_summary_fn

    def collect_single(self, corpus_file: Path) -> CoverageRunResult:
        corpus_hash = _content_hash(corpus_file)
        stored_input = self._raw_dir / corpus_hash
        shutil.copy2(corpus_file, stored_input)
        cov_data = self._collect_single_fn(self.harness_name, corpus_file)
        raw_cov_path = self._raw_dir / f"{corpus_hash}.cov"
        raw_cov_path.write_text(json.dumps(cov_data, indent=2, sort_keys=True))
        return CoverageRunResult(coverage_data=cov_data, raw_cov_path=raw_cov_path)

    def collect_batch_totals(self, corpus_dir: Path) -> dict:
        summary_path = self._collect_batch_fn(Path(self.harness_name), corpus_dir)
        return self._parse_summary_fn(summary_path)


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
        ordered_files = sorted(
            corpus_files,
            key=lambda corpus_file: (_content_hash(corpus_file), corpus_file.name),
        )
        for index, corpus_file in enumerate(ordered_files):
            shards[index % len(self.sessions)].append(corpus_file)

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
        return self.sessions[0].collect_batch_totals(corpus_dir)

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
        self.parse_textcov_output = parse_textcov_output
        self.parse_summary = parse_summary
        self.uniafl_root = Path(uniafl_root or default_uniafl_root()).resolve()
        self.runtime_image = runtime_image or default_uniafl_runtime_image(
            self.language
        )
        self.cpu_set = cpu_set
        self.session_label = session_label
        self._tempdir = tempfile.TemporaryDirectory(prefix="crsbench-uniafl-session-")
        self.workspace = Path(self._tempdir.name)
        self.worker_script_path = self.workspace / "crsbench_cov_worker.py"
        worker_log_stem = (
            "worker" if session_label is None else f"worker.{session_label}"
        )
        self.worker_stdout_path = self.raw_dir / f"{worker_log_stem}.stdout.log"
        self.worker_stderr_path = self.raw_dir / f"{worker_log_stem}.stderr.log"
        self.requests_dir = self.workspace / "requests"
        self.results_dir = self.workspace / "outputs"
        self.container_name = (
            f"crsbench-uniafl-{self.project_name[:20]}-{uuid.uuid4().hex[:10]}"
        )
        self._collected_results: dict[str, CoverageRunResult] = {}
        self._worker_process: Optional[subprocess.Popen[str]] = None
        self._write_worker_script()
        self._start_container()
        self._prepare_harness()
        self._start_worker()

    @property
    def coverage_out_dir(self) -> Path:
        """Coverage-specific build output directory for native targets."""
        return self.build_output_dir / "coverage-out"

    def build_output_mount_dir(self) -> Path:
        """Return the build output directory the coverage runtime should mount."""
        if self.language == "jvm":
            return self.build_output_dir
        return self.coverage_out_dir

    def build_run_once_command(self, seed_paths: list[Path]) -> list[str]:
        """Build the fixed-input `run_once` command for the harness."""
        return ["run_once", self.harness_name] + [str(path) for path in seed_paths]

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
            f"{self.benchmark_path}:/src",
            "-v",
            f"{self.source_repo_dir}:/src/repo",
            "-v",
            f"{self.build_output_dir.resolve()}:/out",
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

    def _start_worker(self) -> None:
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        stdout_handle = self.worker_stdout_path.open("w")
        stderr_handle = self.worker_stderr_path.open("w")
        per_input_timeout = "300" if self.language == "jvm" else "5"
        cmd = [
            "docker",
            "exec",
            "-i",
            "-e",
            f"CRSBENCH_PER_INPUT_TIMEOUT={per_input_timeout}",
            self.container_name,
            "python3",
            "/workspace/crsbench_cov_worker.py",
            "serve",
            self.harness_name,
            "/workspace/requests",
            "/workspace/outputs",
        ]
        self._worker_process = subprocess.Popen(
            cmd,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
        )

    def _ensure_worker_alive(self) -> None:
        if self._worker_process is None:
            raise RuntimeError("UniAFL coverage worker is not running")
        if self._worker_process.poll() is not None:
            raise RuntimeError(
                "UniAFL coverage worker exited unexpectedly. "
                f"See {self.worker_stdout_path} and {self.worker_stderr_path}"
            )

    def _wait_for_result(self, corpus_hash: str) -> CoverageRunResult:
        status_path = self.results_dir / f"{corpus_hash}.status.json"
        cov_path = self.results_dir / f"{corpus_hash}.cov"
        deadline = time.monotonic() + WARM_WORKER_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            self._ensure_worker_alive()
            if status_path.exists():
                break
            time.sleep(0.1)
        else:
            raise RuntimeError(
                f"Timed out waiting for UniAFL coverage result for {corpus_hash}"
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
            candidate = self.results_dir / f"{corpus_hash}.{suffix}"
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

    def collect_single(self, corpus_file: Path) -> CoverageRunResult:
        corpus_hash = _content_hash(corpus_file)
        if corpus_hash in self._collected_results:
            return self._collected_results[corpus_hash]

        staged_seed = self.requests_dir / corpus_hash
        shutil.copy2(corpus_file, staged_seed)
        result = self._wait_for_result(corpus_hash)
        self._collected_results[corpus_hash] = result
        return result

    def collect_many(self, corpus_files: list[Path]) -> dict[Path, CoverageRunResult]:
        return {
            corpus_file: self.collect_single(corpus_file)
            for corpus_file in corpus_files
        }

    def collect_batch_totals(self, corpus_dir: Path) -> dict:
        try:
            batch_session = DockerCoverageSession(
                project_name=self.project_name,
                harness_name=self.harness_name,
                language=self.language,
                build_output_dir=self.build_output_dir,
                output_dir=self.output_dir / ".batch-totals",
                parse_single_output=self.parse_single_output,
                parse_textcov_output=self.parse_textcov_output,
                parse_summary=self.parse_summary,
            )
            try:
                return batch_session.collect_batch_totals(corpus_dir)
            finally:
                batch_session.close()
        except Exception as exc:
            logger.warning(
                "Falling back to approximate UniAFL totals for %s/%s: %s",
                self.project_name,
                self.harness_name,
                exc,
            )
        merged_lines: set[tuple[str, int]] = set()
        covered_functions: set[str] = set()
        covered_sources: set[Path] = set()
        for result in self._collected_results.values():
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
        lines_percent = (
            (lines_covered / lines_total * 100.0) if lines_total > 0 else 0.0
        )
        return {
            "lines_covered": lines_covered,
            "lines_total": lines_total,
            "lines_percent": lines_percent,
            "functions_covered": functions_covered,
            "functions_total": 0,
        }

    def close(self) -> None:
        if self._worker_process is not None and self._worker_process.poll() is None:
            self._worker_process.terminate()
            try:
                self._worker_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._worker_process.kill()
                self._worker_process.wait(timeout=10)
        subprocess.run(
            ["docker", "rm", "-f", self.container_name],
            capture_output=True,
            text=True,
        )
        self._tempdir.cleanup()


class DockerCoverageSession(CoverageSession):
    """Warm Docker-backed session using the base-runner ``coverage`` script."""

    def __init__(
        self,
        *,
        project_name: str,
        harness_name: str,
        language: str,
        build_output_dir: Path,
        output_dir: Path,
        parse_single_output: Callable[[Any], CoverageData],
        parse_textcov_output: Optional[Callable[[Path], CoverageData]],
        parse_summary: Callable[[Path], dict],
    ):
        self.project_name = project_name
        self.harness_name = harness_name
        self.language = language.lower()
        self.build_output_dir = Path(build_output_dir)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir = self.output_dir / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.parse_single_output = parse_single_output
        self.parse_textcov_output = parse_textcov_output
        self.parse_summary = parse_summary

        self._tempdir = tempfile.TemporaryDirectory(prefix="crsbench-cov-session-")
        self.workspace = Path(self._tempdir.name)
        self.inputs_dir = self.workspace / "inputs"
        self.outputs_dir = self.workspace / "outputs"
        self.corpus_root = self.workspace / "corpus"
        self.toolchain_dir = self.workspace / "toolchain"
        self.inputs_dir.mkdir(parents=True, exist_ok=True)
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        self.corpus_root.mkdir(parents=True, exist_ok=True)
        self.toolchain_dir.mkdir(parents=True, exist_ok=True)

        self.container_name = (
            f"crsbench-cov-{self.project_name[:24]}-{uuid.uuid4().hex[:10]}"
        )
        self.image_name = BASE_RUNNER_IMAGE
        self._prepare_runtime_toolchain()
        self._start_container()

    @property
    def fuzzing_language(self) -> str:
        if self.language == "c":
            return "c++"
        return self.language

    def _prepare_runtime_toolchain(self) -> None:
        if self.language not in NATIVE_COVERAGE_LANGUAGES:
            return

        tool_bin_dir = self.toolchain_dir / "bin"
        tool_bin_dir.mkdir(parents=True, exist_ok=True)
        tool_images = (
            default_uniafl_builder_image(),
            default_uniafl_clang_image(),
        )
        errors: list[str] = []
        for tool_image in tool_images:
            tool_container = f"crsbench-cov-tools-{uuid.uuid4().hex[:10]}"
            create_cmd = ["docker", "create", "--name", tool_container, tool_image]
            create_result = subprocess.run(create_cmd, capture_output=True, text=True)
            if create_result.returncode != 0:
                errors.append(
                    f"{tool_image}: {create_result.stderr.strip() or create_result.stdout.strip()}"
                )
                continue

            try:
                for tool_name in ("llvm-profdata", "llvm-cov"):
                    copy_cmd = [
                        "docker",
                        "cp",
                        f"{tool_container}:/usr/local/bin/{tool_name}",
                        str(tool_bin_dir / tool_name),
                    ]
                    copy_result = subprocess.run(
                        copy_cmd, capture_output=True, text=True
                    )
                    if copy_result.returncode != 0:
                        raise RuntimeError(
                            f"Failed to copy {tool_name} from {tool_image}: "
                            f"{copy_result.stderr.strip()}"
                        )
                return
            except RuntimeError as exc:
                errors.append(str(exc))
            finally:
                subprocess.run(
                    ["docker", "rm", "-f", tool_container],
                    capture_output=True,
                    text=True,
                )

        raise RuntimeError(
            "Failed to prepare Atlantis LLVM coverage tools: " + "; ".join(errors)
        )

    def _start_container(self) -> None:
        cmd = [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            self.container_name,
            "-v",
            f"{self.workspace}:/workspace",
            "-v",
            f"{self.build_output_dir}:/out",
            self.image_name,
            "sh",
            "-c",
            "tail -f /dev/null",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to start coverage container {self.image_name}: "
                f"{result.stderr.strip()}"
            )

    def _exec(self, script: str, *, timeout: int = 300) -> tuple[str, str, int, bool]:
        cmd = [
            "docker",
            "exec",
            self.container_name,
            "bash",
            "-lc",
            script,
        ]
        return run_with_graceful_timeout(
            cmd,
            timeout=timeout,
            grace_period=30,
        )

    def _stage_single_input(self, corpus_file: Path) -> tuple[str, Path, Path]:
        corpus_hash = _content_hash(corpus_file)
        raw_input_path = self.raw_dir / corpus_hash
        shutil.copy2(corpus_file, raw_input_path)

        staged_dir = self.corpus_root / self.harness_name
        shutil.rmtree(staged_dir, ignore_errors=True)
        staged_dir.mkdir(parents=True, exist_ok=True)
        staged_input = staged_dir / corpus_file.name
        shutil.copy2(corpus_file, staged_input)
        return corpus_hash, raw_input_path, staged_input

    def _stage_batch_inputs(self, corpus_dir: Path) -> Path:
        staged_dir = self.corpus_root / self.harness_name
        shutil.rmtree(staged_dir, ignore_errors=True)
        staged_dir.mkdir(parents=True, exist_ok=True)
        for path in sorted(corpus_dir.iterdir()):
            if path.is_file() and not path.name.startswith("."):
                shutil.copy2(path, staged_dir / path.name)
        return staged_dir

    def _run_coverage_script(
        self, output_subdir: str, *, timeout: int
    ) -> tuple[str, str, int, bool]:
        path_prefix = ""
        if self.language in NATIVE_COVERAGE_LANGUAGES:
            path_prefix = "export PATH=/workspace/toolchain/bin:$PATH; "
        script = (
            f"{path_prefix}export CORPUS_DIR=/workspace/corpus; "
            f"export COVERAGE_OUTPUT_DIR=/workspace/outputs/{shlex.quote(output_subdir)}; "
            f"export COVERAGE_EXTRA_ARGS=''; "
            f"export FUZZING_LANGUAGE={shlex.quote(self.fuzzing_language)}; "
            f"export FUZZING_ENGINE=libfuzzer; "
            f"export SANITIZER=coverage; "
            f'rm -rf "$COVERAGE_OUTPUT_DIR"; mkdir -p "$COVERAGE_OUTPUT_DIR"; '
            f"coverage {shlex.quote(self.harness_name)}"
        )
        return self._exec(script, timeout=timeout)

    def _collect_crash_log(
        self,
        corpus_hash: str,
        run_output_dir: Path,
        stderr_path: Path,
    ) -> tuple[bool, Optional[Path]]:
        candidates = sorted(run_output_dir.glob("fuzzer_stats/*_error.log"))
        if candidates:
            crash_log_path = self.raw_dir / f"{corpus_hash}.crash.log"
            shutil.copy2(candidates[0], crash_log_path)
            return True, crash_log_path

        stderr_text = stderr_path.read_text() if stderr_path.exists() else ""
        if any(
            token in stderr_text
            for token in (
                "ERROR: AddressSanitizer",
                "ERROR: libFuzzer",
                "Java Exception",
            )
        ):
            crash_log_path = self.raw_dir / f"{corpus_hash}.crash.log"
            crash_log_path.write_text(stderr_text)
            return True, crash_log_path

        return False, None

    def _export_llvm_json(
        self,
        corpus_hash: str,
        run_output_dir: Path,
    ) -> tuple[CoverageData, Optional[Path]]:
        profdata_file = run_output_dir / "dumps" / f"{self.harness_name}.profdata"
        if not profdata_file.exists():
            return {}, None

        shared_libs_cmd = (
            f"shared_libs=$(coverage_helper shared_libs -build-dir=/out "
            f"-object={shlex.quote(self.harness_name)}); "
            f"llvm-cov export -instr-profile={shlex.quote(str(Path('/workspace') / 'outputs' / corpus_hash / 'dumps' / f'{self.harness_name}.profdata'))} "
            f"-object=/out/{shlex.quote(self.harness_name)} "
            f"$shared_libs -ignore-filename-regex='.*src/libfuzzer/.*'"
        )
        stdout, stderr, returncode, timed_out = self._exec(shared_libs_cmd, timeout=300)
        export_path = self.raw_dir / f"{corpus_hash}.llvm-export.json"
        export_path.write_text(stdout if stdout else "{}")
        if timed_out or returncode != 0:
            logger.warning(
                "llvm-cov export failed for %s/%s: %s",
                self.project_name,
                self.harness_name,
                stderr[:300],
            )
            return {}, export_path
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return {}, export_path
        return self.parse_single_output(data), export_path

    def _parse_native_textcov(
        self,
        corpus_hash: str,
        run_output_dir: Path,
    ) -> tuple[CoverageData, Optional[Path]]:
        if self.parse_textcov_output is None:
            return {}, None

        textcov_dir = run_output_dir / "textcov_reports"
        covreport_files = sorted(textcov_dir.glob("*.covreport"))
        if not covreport_files:
            return {}, None

        covreport_path = covreport_files[0]
        raw_covreport_path = self.raw_dir / f"{corpus_hash}.covreport"
        shutil.copy2(covreport_path, raw_covreport_path)
        return self.parse_textcov_output(covreport_path), raw_covreport_path

    def collect_single(self, corpus_file: Path) -> CoverageRunResult:
        corpus_hash, _, _ = self._stage_single_input(corpus_file)
        stdout, stderr, returncode, timed_out = self._run_coverage_script(
            corpus_hash, timeout=300
        )
        self._exec(
            f"chown -R {os.getuid()}:{os.getgid()} /workspace/outputs/{shlex.quote(corpus_hash)} || true",
            timeout=60,
        )
        run_output_dir = self.outputs_dir / corpus_hash
        artifacts_dir = self.raw_dir / f"{corpus_hash}.artifacts"
        if artifacts_dir.exists():
            shutil.rmtree(artifacts_dir)
        if run_output_dir.exists():
            shutil.copytree(run_output_dir, artifacts_dir)
        else:
            artifacts_dir.mkdir(parents=True, exist_ok=True)

        stdout_path = self.raw_dir / f"{corpus_hash}.stdout.log"
        stderr_path = self.raw_dir / f"{corpus_hash}.stderr.log"
        stdout_path.write_text(stdout)
        stderr_path.write_text(stderr)

        crashed, crash_log_path = self._collect_crash_log(
            corpus_hash, run_output_dir, stderr_path
        )
        crashed = crashed or timed_out or returncode != 0

        raw_cov_path = self.raw_dir / f"{corpus_hash}.cov"
        if self.language in ("c", "cpp", "c++", "rust", "go"):
            cov_data, _ = self._parse_native_textcov(corpus_hash, run_output_dir)
            if not cov_data:
                cov_data, _ = self._export_llvm_json(corpus_hash, run_output_dir)
        else:
            jacoco_xml = run_output_dir / "report" / "linux" / "jacoco.xml"
            cov_data = (
                self.parse_single_output(jacoco_xml) if jacoco_xml.exists() else {}
            )
        raw_cov_path.write_text(json.dumps(cov_data, indent=2, sort_keys=True))

        return CoverageRunResult(
            coverage_data=cov_data,
            raw_cov_path=raw_cov_path,
            raw_artifacts_dir=artifacts_dir,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            crashed=crashed,
            crash_log_path=crash_log_path,
        )

    def collect_batch_totals(self, corpus_dir: Path) -> dict:
        self._stage_batch_inputs(corpus_dir)
        output_subdir = "batch"
        stdout, stderr, returncode, timed_out = self._run_coverage_script(
            output_subdir, timeout=7200
        )
        self._exec(
            f"chown -R {os.getuid()}:{os.getgid()} /workspace/outputs/{shlex.quote(output_subdir)} || true",
            timeout=60,
        )
        if timed_out or returncode != 0:
            raise RuntimeError(
                f"Batch coverage failed for {self.project_name}/{self.harness_name}: "
                f"{stderr[:500]}"
            )
        if self.language in NATIVE_COVERAGE_LANGUAGES:
            summary_path = (
                self.outputs_dir / output_subdir / "report" / "linux" / "summary.json"
            )
        else:
            summary_path = (
                self.outputs_dir / output_subdir / "report" / "linux" / "jacoco.xml"
            )
        if not summary_path.exists():
            raise RuntimeError(
                f"Coverage summary not found for {self.project_name}/{self.harness_name}"
            )
        return self.parse_summary(summary_path)

    def close(self) -> None:
        subprocess.run(
            ["docker", "rm", "-f", self.container_name],
            capture_output=True,
            text=True,
        )
        self._tempdir.cleanup()


class JazzerWarmCoverageSession(DockerCoverageSession):
    """Warm JVM coverage session backed by one long-lived per-harness worker."""

    def __init__(
        self,
        *,
        project_name: str,
        harness_name: str,
        language: str,
        build_output_dir: Path,
        output_dir: Path,
        parse_single_output: Callable[[Any], CoverageData],
        parse_textcov_output: Optional[Callable[[Path], CoverageData]] = None,
        parse_summary: Callable[[Path], dict],
    ):
        super().__init__(
            project_name=project_name,
            harness_name=harness_name,
            language=language,
            build_output_dir=build_output_dir,
            output_dir=output_dir,
            parse_single_output=parse_single_output,
            parse_textcov_output=parse_textcov_output,
            parse_summary=parse_summary,
        )
        self.requests_dir = self.workspace / "worker-requests"
        self.results_dir = self.workspace / "worker-results"
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.worker_start_count = 0
        self._worker_started = False
        self._worker_process: Optional[subprocess.Popen] = None
        self._worker_stdout_path = self.raw_dir / "worker.stdout.log"
        self._worker_stderr_path = self.raw_dir / "worker.stderr.log"

    def _start_worker_process(self) -> None:
        harness_wrapper = Path("/out") / self.harness_name
        cmd = [
            "docker",
            "exec",
            "-i",
            self.container_name,
            "bash",
            "-lc",
            " ".join(
                [
                    shlex.quote(str(harness_wrapper)),
                    "--crsbench_warm_coverage",
                    "--crsbench_request_dir=/workspace/worker-requests",
                    "--crsbench_result_dir=/workspace/worker-results",
                ]
            ),
        ]
        stdout_handle = self._worker_stdout_path.open("w")
        stderr_handle = self._worker_stderr_path.open("w")
        self._worker_process = subprocess.Popen(
            cmd,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
        )
        self.worker_start_count += 1
        self._worker_started = True

    def _ensure_worker_started(self) -> None:
        if not self._worker_started:
            self._start_worker_process()
            return
        if self._worker_process is not None and self._worker_process.poll() is not None:
            raise RuntimeError(
                "Warm Jazzer coverage worker exited unexpectedly. "
                f"See {self._worker_stdout_path} and {self._worker_stderr_path}"
            )

    def _wait_for_worker_artifacts(
        self, corpus_hash: str
    ) -> tuple[Path, Path, Optional[Path]]:
        cov_path = self.results_dir / f"{corpus_hash}.cov"
        status_path = self.results_dir / f"{corpus_hash}.status.json"
        crash_path = self.results_dir / f"{corpus_hash}.crash.log"
        deadline = time.monotonic() + WARM_WORKER_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            self._ensure_worker_started()
            if cov_path.exists() and status_path.exists():
                return (
                    cov_path,
                    status_path,
                    crash_path if crash_path.exists() else None,
                )
            time.sleep(0.1)
        raise RuntimeError(
            f"Timed out waiting for warm Jazzer coverage output for {corpus_hash}. "
            f"See {self._worker_stdout_path} and {self._worker_stderr_path}"
        )

    def _collect_single_from_worker(
        self, corpus_hash: str, corpus_file: Path
    ) -> CoverageRunResult:
        request_path = self.requests_dir / corpus_hash
        request_tmp = self.requests_dir / f"{corpus_hash}.tmp"
        request_tmp.write_bytes(corpus_file.read_bytes())
        request_tmp.replace(request_path)
        cov_path, status_path, crash_path = self._wait_for_worker_artifacts(corpus_hash)
        coverage_data = json.loads(cov_path.read_text())
        status_data = json.loads(status_path.read_text())

        raw_cov_path = self.raw_dir / f"{corpus_hash}.cov"
        shutil.copy2(cov_path, raw_cov_path)
        copied_crash_log: Optional[Path] = None
        if crash_path is not None and crash_path.exists():
            copied_crash_log = self.raw_dir / f"{corpus_hash}.crash.log"
            shutil.copy2(crash_path, copied_crash_log)

        return CoverageRunResult(
            coverage_data=coverage_data,
            raw_cov_path=raw_cov_path,
            crashed=bool(status_data.get("crashed", False)),
            crash_log_path=copied_crash_log,
        )

    def collect_single(self, corpus_file: Path) -> CoverageRunResult:
        self._ensure_worker_started()
        corpus_hash, _, _ = self._stage_single_input(corpus_file)
        return self._collect_single_from_worker(corpus_hash, corpus_file)

    def close(self) -> None:
        if self._worker_process is not None and self._worker_process.poll() is None:
            self._worker_process.terminate()
            try:
                self._worker_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._worker_process.kill()
                self._worker_process.wait(timeout=5)
        super().close()


def _content_hash(path: Path) -> str:
    sha256 = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(8192):
            sha256.update(chunk)
    return sha256.hexdigest()[:16]


_UNIAFL_COVERAGE_WORKER_SCRIPT = r"""#!/usr/bin/env python3
import base64
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
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


def _run(harness_name: str, output_dir: str, inputs: list[str]) -> int:
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
    return _run(harness_name, output_dir, inputs)


def _serve(harness_name: str, request_dir: str, output_dir: str) -> int:
    Config(0, 1).load("/crs.config")
    cp = init_cp_in_runner()
    harness = cp.get_harnesses()[harness_name]
    request_root = Path(request_dir)
    output_root = Path(output_dir)
    request_root.mkdir(parents=True, exist_ok=True)
    output_root.mkdir(parents=True, exist_ok=True)
    while True:
        for blob_path in sorted(request_root.iterdir()):
            if not blob_path.is_file() or blob_path.name.startswith("."):
                continue
            seed_name = blob_path.name
            if (output_root / f"{seed_name}.status.json").exists():
                continue
            _process_one_input(harness_name, harness, blob_path, output_root)
            blob_path.unlink(missing_ok=True)
        time.sleep(0.1)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(
            "usage: crsbench_cov_worker.py prepare|run|run-dir|serve <harness> ..."
        )
    command = sys.argv[1]
    harness = sys.argv[2]
    if command == "prepare":
        raise SystemExit(_prepare(harness))
    if command == "run":
        raise SystemExit(_run(harness, sys.argv[3], sys.argv[4:]))
    if command == "run-dir":
        raise SystemExit(_run_dir(harness, sys.argv[3], sys.argv[4]))
    if command == "serve":
        raise SystemExit(_serve(harness, sys.argv[3], sys.argv[4]))
    raise SystemExit(f"unknown command: {command}")
"""
