"""Interactive experiment config generator.

Prompts users for required fields and generates a valid grouped-format
experiment config YAML file. Uses questionary for arrow-key selection UI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import questionary
import yaml
from questionary import Choice

# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------


def discover_benchmark_suites(
    suites_root: Path = Path("benchmark-suites"),
) -> List[Dict[str, Any]]:
    """Return list of available benchmark suites with metadata.

    Args:
        suites_root: Directory containing benchmark suite YAML files.

    Returns:
        List of dicts with keys: name, description, count.
    """
    if not suites_root.is_dir():
        return []
    suites = []
    for p in sorted(suites_root.glob("*.yaml")):
        meta: Dict[str, Any] = {"name": p.stem, "description": "", "count": 0}
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                meta["description"] = data.get("Description", "")
                bl = data.get("benchmark_list", [])
                meta["count"] = len(bl) if isinstance(bl, list) else 0
        except Exception:  # noqa: BLE001
            pass
        suites.append(meta)
    return suites


def discover_crs_registry(
    registry_dir: Path = Path("oss-crs/registry"),
) -> List[Dict[str, Any]]:
    """Return list of registered CRS entries with metadata.

    Args:
        registry_dir: Directory containing CRS registry YAML files.

    Returns:
        List of dicts with keys: name, types.
    """
    if not registry_dir.is_dir():
        return []
    entries = []
    for p in sorted(registry_dir.glob("*.yaml")):
        meta: Dict[str, Any] = {"name": p.stem, "types": []}
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                types = data.get("type", [])
                meta["types"] = types if isinstance(types, list) else [types]
        except Exception:  # noqa: BLE001
            pass
        entries.append(meta)
    return entries


def discover_crs_names(
    registry_dir: Path = Path("oss-crs/registry"),
) -> List[str]:
    """Return sorted list of registered CRS names (thin wrapper for tests).

    Args:
        registry_dir: Directory containing CRS registry YAML files.

    Returns:
        List of CRS names (filename stems).
    """
    return [e["name"] for e in discover_crs_registry(registry_dir)]


# ---------------------------------------------------------------------------
# Interactive prompting (questionary-based)
# ---------------------------------------------------------------------------

_TASK_CHOICES = [
    Choice(
        title="bugfinding  — CRS discovers vulnerabilities by generating POVs",
        value="bugfinding",
    ),
    Choice(
        title="bugfixing   — CRS generates patches for known vulnerabilities",
        value="bugfixing",
    ),
]

_MODE_CHOICES = [
    Choice(
        title="delta — evaluate on code diffs between two commits (most common)",
        value="delta",
    ),
    Choice(
        title="full  — evaluate on a single vulnerable commit snapshot",
        value="full",
    ),
    Choice(
        title="all   — run both delta and full modes for each benchmark",
        value="all",
    ),
    Choice(
        title="auto  — pick available mode per benchmark (delta preferred)",
        value="auto",
    ),
]

_LITELLM_MODE_CHOICES = [
    Choice(
        title="external  — use an external LiteLLM proxy (most common)",
        value="external",
    ),
    Choice(
        title="skip      — no LLM needed (pure fuzzer CRS like crs-libfuzzer)",
        value="skip",
    ),
]

_SANITIZER_CHOICES = [
    Choice(
        title="address   — AddressSanitizer (heap/stack buffer overflows, use-after-free)",
        value="address",
    ),
    Choice(
        title="memory    — MemorySanitizer (uninitialized memory reads)",
        value="memory",
    ),
    Choice(
        title="undefined — UBSan (undefined behavior: signed overflow, null deref, etc.)",
        value="undefined",
    ),
]


def prompt_config_interactive(
    suites_root: Path = Path("benchmark-suites"),
    registry_dir: Path = Path("oss-crs/registry"),
) -> Dict[str, Any]:
    """Interactively prompt for experiment config values.

    Uses questionary for arrow-key navigable selection menus
    with descriptions for each option.

    Args:
        suites_root: Benchmark suites directory.
        registry_dir: CRS registry directory.

    Returns:
        Config dict in grouped format ready for YAML serialization.
    """
    print("\n=== CRSBench Experiment Config Generator ===\n")

    # --- Experiment section ---
    print("-- Experiment --")

    name = questionary.text(
        "Experiment name:",
        instruction="(unique identifier for this experiment run)",
        validate=lambda v: True if v.strip() else "Experiment name is required",
    ).unsafe_ask()

    description = questionary.text(
        "Description:",
        instruction="(optional — human-readable note for reports, leave empty to skip)",
    ).unsafe_ask()
    description = description.strip() or None

    task = questionary.select(
        "Task type:",
        choices=_TASK_CHOICES,
        default="bugfinding",
        instruction="(use arrow keys)",
    ).unsafe_ask()

    mode = questionary.select(
        "Evaluation mode:",
        choices=_MODE_CHOICES,
        default="delta",
        instruction="(use arrow keys)",
    ).unsafe_ask()

    # Benchmark selection
    available_suites = discover_benchmark_suites(suites_root)

    use_suite = questionary.confirm(
        "Use a benchmark suite (rather than listing individual benchmarks)?",
        default=True,
        instruction="(suites are pre-defined benchmark lists)",
    ).unsafe_ask()

    benchmark_suite: Optional[str] = None
    benchmarks: Optional[List[str]] = None

    if use_suite and available_suites:
        suite_choices = [
            Choice(
                title=f"{s['name']:30s} — {s['description']} ({s['count']} benchmarks)"
                if s["description"]
                else f"{s['name']:30s} ({s['count']} benchmarks)",
                value=s["name"],
            )
            for s in available_suites
        ]
        benchmark_suite = questionary.select(
            "Benchmark suite:",
            choices=suite_choices,
            instruction="(use arrow keys)",
        ).unsafe_ask()
    elif use_suite and not available_suites:
        print("  No benchmark suites found in", suites_root)
        print("  Falling back to manual benchmark entry.\n")
        use_suite = False

    if not use_suite:
        raw = questionary.text(
            "Benchmarks (comma-separated):",
            instruction="(e.g., afc-curl-delta-05, afc-libxml2-delta-03)",
            validate=lambda v: True if v.strip() else "At least one benchmark required",
        ).unsafe_ask()
        benchmarks = [b.strip() for b in raw.split(",") if b.strip()]

    sanitizers = questionary.checkbox(
        "Sanitizers:",
        choices=_SANITIZER_CHOICES,
        instruction="(space to toggle, enter to confirm)",
        validate=lambda v: True if v else "Select at least one sanitizer",
    ).unsafe_ask()

    # --- CRS Compose section ---
    print("\n-- CRS Configuration --")
    available_crs = discover_crs_registry(registry_dir)

    crs_services: Dict[str, Any] = {}
    while True:
        if available_crs:
            crs_choices = []
            for entry in available_crs:
                types_str = ", ".join(entry["types"]) if entry["types"] else "unknown"
                already = " (already selected)" if entry["name"] in crs_services else ""
                crs_choices.append(
                    Choice(
                        title=f"{entry['name']:40s} [{types_str}]{already}",
                        value=entry["name"],
                    ),
                )
            crs_choices.append(
                Choice(title="(enter custom CRS name)", value="__custom__"),
            )
            if crs_services:
                crs_choices.append(
                    Choice(title="(done — no more CRS to add)", value="__done__"),
                )
            selected = questionary.select(
                "Select CRS:" if not crs_services else "Add another CRS:",
                choices=crs_choices,
                instruction="(use arrow keys)",
            ).unsafe_ask()
            if selected == "__done__":
                break
            if selected == "__custom__":
                crs_name = questionary.text(
                    "Custom CRS name:",
                    validate=lambda v: True if v.strip() else "CRS name is required",
                ).unsafe_ask()
            else:
                crs_name = selected
        else:
            crs_name = questionary.text(
                "CRS name:" if not crs_services else "CRS name (empty to finish):",
            ).unsafe_ask()
            if not crs_name.strip():
                if crs_services:
                    break
                print("  At least one CRS is required.")
                continue
            crs_name = crs_name.strip()

        num_cores_str = questionary.text(
            f"  CPU cores for '{crs_name}':",
            default="8",
            instruction="(integer, minimum 1)",
            validate=_validate_positive_int,
        ).unsafe_ask()

        mem_limit = questionary.text(
            f"  Memory limit for '{crs_name}':",
            default="",
            instruction="(e.g., 8G, 16G — leave empty for unlimited)",
        ).unsafe_ask()

        service_config: Dict[str, Any] = {"num_cores": int(num_cores_str)}
        if mem_limit.strip():
            service_config["mem_limit"] = mem_limit.strip()
        crs_services[crs_name] = service_config

        if not questionary.confirm("  Add another CRS?", default=False).unsafe_ask():
            break

    # --- Runtime section ---
    print("\n-- Runtime --")

    trials_str = questionary.text(
        "Number of trials:",
        default="1",
        instruction="(how many times to repeat the experiment)",
        validate=_validate_positive_int,
    ).unsafe_ask()
    trials = int(trials_str)

    max_total_str = questionary.text(
        "Max total time per trial (seconds):",
        default="28800",
        instruction="(28800 = 8 hours — upper bound for build+run+verify)",
        validate=_validate_positive_int,
    ).unsafe_ask()
    max_total_time = int(max_total_str)

    build_str = questionary.text(
        "Build timeout (seconds):",
        default="3600",
        instruction="(3600 = 1 hour — CRS Docker image build phase)",
        validate=_validate_positive_int,
    ).unsafe_ask()
    build_timeout = int(build_str)

    run_str = questionary.text(
        "Run timeout (seconds):",
        default="14400",
        instruction="(14400 = 4 hours — CRS execution phase)",
        validate=_validate_positive_int,
    ).unsafe_ask()
    run_timeout = int(run_str)

    verify_str = questionary.text(
        "Verify timeout (seconds):",
        default="7200",
        instruction="(7200 = 2 hours — POV/patch verification phase)",
        validate=_validate_positive_int,
    ).unsafe_ask()
    verify_timeout = int(verify_str)

    pov_early_stop = False
    if task == "bugfinding":
        pov_early_stop = questionary.confirm(
            "Enable POV early stop?",
            default=False,
            instruction="(stop trial early when all CPVs for a harness are confirmed)",
        ).unsafe_ask()

    # --- LiteLLM section ---
    print("\n-- LLM Configuration --")

    litellm_selection = questionary.select(
        "LiteLLM mode:",
        choices=_LITELLM_MODE_CHOICES,
        default="external",
        instruction="(use arrow keys)",
    ).unsafe_ask()

    skip_litellm = litellm_selection == "skip"
    litellm_mode: Optional[str] = None if skip_litellm else litellm_selection
    llm_tracking_enabled = True
    litellm_cost_budget: Optional[float] = None

    if not skip_litellm:
        llm_tracking_enabled = questionary.confirm(
            "Enable LLM usage tracking?",
            default=True,
            instruction="(track per-trial cost and token metrics via LiteLLM Virtual Keys)",
        ).unsafe_ask()

        set_budget = questionary.confirm(
            "Set a per-trial LLM cost budget?",
            default=False,
            instruction="(limit LLM spending in USD per trial)",
        ).unsafe_ask()

        if set_budget:
            budget_str = questionary.text(
                "Cost budget (USD):",
                default="10.0",
                instruction="(e.g., 10.0 — CRS key is revoked when budget is exceeded)",
                validate=_validate_positive_float,
            ).unsafe_ask()
            litellm_cost_budget = float(budget_str)

    # --- Storage section ---
    print("\n-- Storage --")

    experiment_filestore = questionary.text(
        "Experiment filestore path:",
        default="./experiment-data",
        instruction="(directory for trial outputs and raw data)",
    ).unsafe_ask()

    report_filestore = questionary.text(
        "Report filestore path:",
        default="./report-data",
        instruction="(directory for HTML reports and summaries)",
    ).unsafe_ask()

    # --- Distributed / Parallel section ---
    print("\n-- Distributed / Parallel Processing --")

    use_distributed = questionary.confirm(
        "Enable distributed mode (multiple workers/evaluators via Redis)?",
        default=False,
        instruction="(requires a Redis server for job coordination)",
    ).unsafe_ask()

    redis_host: Optional[str] = None
    worker_config: Optional[Dict[str, Any]] = None
    evaluator_config: Optional[Dict[str, Any]] = None

    if use_distributed:
        redis_host = questionary.text(
            "Redis host:",
            default="localhost:6379",
            instruction="(hostname:port for the Redis job queue)",
        ).unsafe_ask()

    # Worker config — always ask
    print("")
    worker_jobs_str = questionary.text(
        "Worker parallel jobs:",
        default="1",
        instruction="(concurrent trial executions per worker process)",
        validate=_validate_positive_int,
    ).unsafe_ask()
    worker_jobs = int(worker_jobs_str)

    worker_cores_str = questionary.text(
        "Worker cores per job:",
        default="8",
        instruction="(CPU cores allocated to each parallel trial)",
        validate=_validate_positive_int,
    ).unsafe_ask()
    worker_cores = int(worker_cores_str)

    worker_config = {"jobs": worker_jobs, "cores_per_job": worker_cores}

    # Evaluator config — always ask
    evaluator_jobs_str = questionary.text(
        "Evaluator parallel jobs:",
        default="4",
        instruction="(concurrent build/verify tasks for POV evaluation)",
        validate=_validate_positive_int,
    ).unsafe_ask()
    evaluator_jobs = int(evaluator_jobs_str)

    evaluator_cores_str = questionary.text(
        "Evaluator cores per job:",
        default="4",
        instruction="(CPU cores allocated to each evaluator task)",
        validate=_validate_positive_int,
    ).unsafe_ask()
    evaluator_cores = int(evaluator_cores_str)

    evaluator_config = {"jobs": evaluator_jobs, "cores_per_job": evaluator_cores}

    # --- Build config dict ---
    return _assemble_config(
        name=name,
        description=description,
        task=task,
        mode=mode,
        benchmark_suite=benchmark_suite,
        benchmarks=benchmarks,
        sanitizers=sanitizers,
        crs_services=crs_services,
        trials=trials,
        max_total_time=max_total_time,
        build_timeout=build_timeout,
        run_timeout=run_timeout,
        verify_timeout=verify_timeout,
        experiment_filestore=experiment_filestore,
        report_filestore=report_filestore,
        redis_host=redis_host,
        worker=worker_config,
        evaluator=evaluator_config,
        pov_early_stop=pov_early_stop,
        litellm_mode=litellm_mode,
        skip_litellm=skip_litellm,
        llm_tracking_enabled=llm_tracking_enabled,
        litellm_cost_budget=litellm_cost_budget,
    )


def _validate_positive_int(val: str) -> bool | str:
    """Questionary validator for positive integer input."""
    try:
        n = int(val)
    except ValueError:
        return "Please enter a valid integer"
    if n < 1:
        return "Must be >= 1"
    return True


def _validate_positive_float(val: str) -> bool | str:
    """Questionary validator for positive float input."""
    try:
        n = float(val)
    except ValueError:
        return "Please enter a valid number"
    if n <= 0:
        return "Must be > 0"
    return True


def _assemble_config(
    *,
    name: str,
    task: str,
    mode: str,
    benchmark_suite: Optional[str],
    benchmarks: Optional[List[str]],
    sanitizers: List[str],
    crs_services: Dict[str, Any],
    trials: int,
    max_total_time: int,
    build_timeout: int,
    run_timeout: int,
    verify_timeout: int,
    experiment_filestore: str,
    report_filestore: str,
    description: Optional[str] = None,
    redis_host: Optional[str] = None,
    worker: Optional[Dict[str, Any]] = None,
    evaluator: Optional[Dict[str, Any]] = None,
    pov_early_stop: bool = False,
    litellm_mode: Optional[str] = None,
    skip_litellm: bool = False,
    llm_tracking_enabled: bool = True,
    litellm_cost_budget: Optional[float] = None,
) -> Dict[str, Any]:
    """Assemble the grouped config dict from individual values."""
    experiment_block: Dict[str, Any] = {"name": name, "task": task, "mode": mode}
    if benchmark_suite:
        experiment_block["benchmark_suite"] = benchmark_suite
    if benchmarks:
        experiment_block["benchmarks"] = benchmarks
    if sanitizers != ["address"]:
        experiment_block["sanitizers"] = sanitizers

    runtime_block: Dict[str, Any] = {
        "trials": trials,
        "max_total_time": max_total_time,
        "build_timeout": build_timeout,
        "run_timeout": run_timeout,
        "verify_timeout": verify_timeout,
    }
    if redis_host:
        runtime_block["redis_host"] = redis_host
    if pov_early_stop:
        runtime_block["pov_early_stop"] = True

    # LiteLLM settings
    if skip_litellm:
        runtime_block["skip_litellm"] = True
    else:
        litellm_block: Dict[str, Any] = {}
        if litellm_mode:
            litellm_block["mode"] = litellm_mode
        if not llm_tracking_enabled:
            litellm_block["tracking_enabled"] = False
        if litellm_cost_budget is not None:
            litellm_block["cost_budget"] = litellm_cost_budget
        if litellm_block:
            runtime_block["litellm"] = litellm_block

    config: Dict[str, Any] = {}
    if description:
        config["description"] = description
    config["experiment"] = experiment_block
    config["crs_compose"] = crs_services
    config["runtime"] = runtime_block
    config["storage"] = {
        "experiment_filestore": experiment_filestore,
        "report_filestore": report_filestore,
    }

    if worker:
        config["worker"] = worker
    if evaluator:
        config["evaluator"] = evaluator

    return config


# ---------------------------------------------------------------------------
# Non-interactive (programmatic) config builder
# ---------------------------------------------------------------------------


def build_config_from_answers(answers: Dict[str, Any]) -> Dict[str, Any]:
    """Build a grouped config dict from a flat answers dict.

    This is the non-interactive counterpart to prompt_config_interactive(),
    useful for programmatic generation and testing.

    Args:
        answers: Dict with keys matching the prompted fields:
            - name (str, required)
            - description (str|None)
            - task (str, default: "bugfinding")
            - mode (str, default: "delta")
            - benchmark_suite (str|None)
            - benchmarks (list[str]|None)
            - sanitizers (list[str], default: ["address"])
            - crs_services (dict[str, dict], required)
            - trials (int, default: 1)
            - max_total_time (int, default: 28800)
            - build_timeout (int, default: 3600)
            - run_timeout (int, default: 14400)
            - verify_timeout (int, default: 7200)
            - experiment_filestore (str, default: "./experiment-data")
            - report_filestore (str, default: "./report-data")
            - redis_host (str|None)
            - worker (dict|None)
            - evaluator (dict|None)
            - pov_early_stop (bool, default: False)
            - litellm_mode (str|None, default: None)
            - skip_litellm (bool, default: False)
            - llm_tracking_enabled (bool, default: True)
            - litellm_cost_budget (float|None)

    Returns:
        Config dict in grouped format.
    """
    return _assemble_config(
        name=answers["name"],
        description=answers.get("description"),
        task=answers.get("task", "bugfinding"),
        mode=answers.get("mode", "delta"),
        benchmark_suite=answers.get("benchmark_suite"),
        benchmarks=answers.get("benchmarks"),
        sanitizers=answers.get("sanitizers", ["address"]),
        crs_services=answers.get("crs_services", {}),
        trials=answers.get("trials", 1),
        max_total_time=answers.get("max_total_time", 28800),
        build_timeout=answers.get("build_timeout", 3600),
        run_timeout=answers.get("run_timeout", 14400),
        verify_timeout=answers.get("verify_timeout", 7200),
        experiment_filestore=answers.get("experiment_filestore", "./experiment-data"),
        report_filestore=answers.get("report_filestore", "./report-data"),
        redis_host=answers.get("redis_host"),
        worker=answers.get("worker"),
        evaluator=answers.get("evaluator"),
        pov_early_stop=answers.get("pov_early_stop", False),
        litellm_mode=answers.get("litellm_mode"),
        skip_litellm=answers.get("skip_litellm", False),
        llm_tracking_enabled=answers.get("llm_tracking_enabled", True),
        litellm_cost_budget=answers.get("litellm_cost_budget"),
    )


# ---------------------------------------------------------------------------
# YAML rendering
# ---------------------------------------------------------------------------


def render_config_yaml(config: Dict[str, Any]) -> str:
    """Render config dict as YAML string with section comments.

    Args:
        config: Config dict in grouped format.

    Returns:
        YAML string with helpful comments.
    """
    lines = [
        "# CRSBench experiment configuration",
        "# Generated by: crsbench gen-config",
        "#",
        "# Reference: docs/experiment-config-distributed-example.yaml",
        "# Validate with: crsbench gen-config --validate --output <file>",
        "",
    ]

    # Description (top-level, before experiment block)
    if config.get("description"):
        lines.append(
            yaml.dump(
                {"description": config["description"]},
                default_flow_style=False,
                sort_keys=False,
            ).rstrip()
        )
        lines.append("")

    # Experiment section
    lines.append(
        yaml.dump(
            {"experiment": config["experiment"]},
            default_flow_style=None,
            sort_keys=False,
            indent=2,
        ).rstrip()
    )
    lines.append("")

    # CRS compose section
    lines.append("# Per-CRS resource allocation. Adjust num_cores/mem_limit per CRS.")
    lines.append("# Additional options: mem_limit, additional_env")
    lines.append(
        yaml.dump(
            {"crs_compose": config["crs_compose"]},
            default_flow_style=False,
            sort_keys=False,
            indent=2,
        ).rstrip()
    )
    lines.append("")

    # Runtime section
    lines.append("# Runtime settings. For distributed mode, add redis_host.")
    lines.append("# Additional options: per_pov_verify_timeout, snapshot_period,")
    lines.append("#   pov_early_stop, litellm, inputs (pov/sarif/seed/diff)")
    lines.append(
        yaml.dump(
            {"runtime": config["runtime"]},
            default_flow_style=False,
            sort_keys=False,
            indent=2,
        ).rstrip()
    )
    lines.append("")

    # Storage section
    lines.append("# Storage paths. For distributed mode, consider shared mount points.")
    lines.append("# Additional options: keep_only_results, cleanup_after_trial,")
    lines.append("#   copy_results_after_trial, results_filestore")
    lines.append(
        yaml.dump(
            {"storage": config["storage"]},
            default_flow_style=False,
            sort_keys=False,
            indent=2,
        ).rstrip()
    )
    lines.append("")

    # Worker section
    if "worker" in config:
        lines.append("# Worker settings — parallel trial execution capacity.")
        lines.append("# Additional options: continuous, worker_name, minimum_disk_size")
        lines.append(
            yaml.dump(
                {"worker": config["worker"]},
                default_flow_style=False,
                sort_keys=False,
                indent=2,
            ).rstrip()
        )
        lines.append("")

    # Evaluator section
    if "evaluator" in config:
        lines.append("# Evaluator settings — parallel POV build/verify capacity.")
        lines.append("# Additional options: idle_timeout, build_jobs, verify_jobs")
        lines.append(
            yaml.dump(
                {"evaluator": config["evaluator"]},
                default_flow_style=False,
                sort_keys=False,
                indent=2,
            ).rstrip()
        )
        lines.append("")

    # Post-generation hints as comments
    lines.extend(
        [
            "# --- Optional sections (uncomment and configure as needed) ---",
            "",
            "# resources:",
            "#   cores_per_trial: 8",
            '#   memory_per_trial: "16G"',
            "#   cpu_tag: amd-epyc",
            "",
        ]
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def generate_config(
    output_path: Path,
    answers: Optional[Dict[str, Any]] = None,
    *,
    validate: bool = False,
    suites_root: Path = Path("benchmark-suites"),
    registry_dir: Path = Path("oss-crs/registry"),
) -> bool:
    """Generate an experiment config file.

    Args:
        output_path: Path to write the generated YAML.
        answers: Pre-filled answers dict (skips interactive prompts).
        validate: Whether to validate the generated config.
        suites_root: Benchmark suites directory.
        registry_dir: CRS registry directory.

    Returns:
        True if config was generated (and validated if requested) successfully.
    """
    if answers is not None:
        config = build_config_from_answers(answers)
    else:
        config = prompt_config_interactive(
            suites_root=suites_root,
            registry_dir=registry_dir,
        )

    yaml_content = render_config_yaml(config)

    if validate:
        from crsbench.validation.format_validator import (
            validate_experiment_config_from_string,
        )

        result = validate_experiment_config_from_string(yaml_content)
        if not result.is_valid:
            print("\nValidation errors in generated config:")
            for error in result.errors:
                print(f"  - {error.message}")
            print("\nConfig was NOT written. Fix the issues and try again.")
            return False
        print("\nGenerated config passed validation.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml_content, encoding="utf-8")
    print(f"\nConfig written to: {output_path}")
    return True
