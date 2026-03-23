"""Interactive experiment config generator.

Prompts users for required fields and generates a valid grouped-format
experiment config YAML file. Uses InquirerPy for interactive TUI prompts
with arrow-key selection and fuzzy autocomplete.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from humanfriendly import parse_timespan
from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from InquirerPy.validator import NumberValidator

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
# Choice definitions
# ---------------------------------------------------------------------------

_TASK_CHOICES = [
    Choice(
        value="bugfinding",
        name="bugfinding  — CRS discovers vulnerabilities by generating POVs",
    ),
    Choice(
        value="bugfixing",
        name="bugfixing   — CRS generates patches for known vulnerabilities",
    ),
]

_MODE_CHOICES = [
    Choice(
        value="delta",
        name="delta — evaluate on code diffs between two commits (most common)",
    ),
    Choice(
        value="full",
        name="full  — evaluate on a single vulnerable commit snapshot",
    ),
    Choice(
        value="all",
        name="all   — run both delta and full modes for each benchmark",
    ),
    Choice(
        value="auto",
        name="auto  — pick available mode per benchmark (delta preferred)",
    ),
]

_LITELLM_MODE_CHOICES = [
    Choice(
        value="external",
        name="external — use an external LiteLLM proxy (most common)",
    ),
    Choice(
        value="skip",
        name="skip     — no LLM needed (pure fuzzer CRS like crs-libfuzzer)",
    ),
]

_SANITIZER_CHOICES = [
    Choice(
        value="address",
        name="address   — AddressSanitizer (heap/stack buffer overflows, use-after-free)",
        enabled=True,
    ),
    Choice(
        value="memory",
        name="memory    — MemorySanitizer (uninitialized memory reads)",
    ),
    Choice(
        value="undefined",
        name="undefined — UBSan (undefined behavior: signed overflow, null deref, etc.)",
    ),
]


# ---------------------------------------------------------------------------
# Interactive prompting
# ---------------------------------------------------------------------------


def prompt_config_interactive(
    suites_root: Path = Path("benchmark-suites"),
    registry_dir: Path = Path("oss-crs/registry"),
) -> Dict[str, Any]:
    """Interactively prompt for experiment config values.

    Uses InquirerPy for arrow-key navigable selection menus
    with fuzzy autocomplete and descriptions.

    Args:
        suites_root: Benchmark suites directory.
        registry_dir: CRS registry directory.

    Returns:
        Config dict in grouped format ready for YAML serialization.
    """
    print("\n=== CRSBench Experiment Config Generator ===")  # noqa: T201

    # --- Experiment section ---
    print("\n-- Experiment --")  # noqa: T201

    name = inquirer.text(
        message="Experiment name:",
        long_instruction="Unique identifier for this experiment run (used in paths and reports)",
        validate=lambda val: len(val.strip()) > 0,
        invalid_message="Experiment name is required",
    ).execute()

    description_raw = inquirer.text(
        message="Description:",
        default="",
        long_instruction="Optional human-readable note shown in reports. Leave empty to skip",
    ).execute()
    description = description_raw.strip() or None

    task = inquirer.select(
        message="Task type:",
        choices=_TASK_CHOICES,
        default="bugfinding",
        long_instruction="Determines what the CRS is expected to produce (POVs vs patches)",
    ).execute()

    mode = inquirer.select(
        message="Evaluation mode:",
        choices=_MODE_CHOICES,
        default="delta",
        long_instruction="Controls which benchmark commits are used for evaluation",
    ).execute()

    # Benchmark selection
    available_suites = discover_benchmark_suites(suites_root)

    use_suite = inquirer.confirm(
        message="Use a benchmark suite (pre-defined benchmark list)?",
        default=True,
        long_instruction="Suites are curated lists of benchmarks. Otherwise enter names manually",
    ).execute()

    benchmark_suite: Optional[str] = None
    benchmarks: Optional[List[str]] = None

    if use_suite and available_suites:
        suite_choices = [
            Choice(
                value=s["name"],
                name=f"{s['name']:30s} — {s['description']} ({s['count']} benchmarks)"
                if s["description"]
                else f"{s['name']:30s} ({s['count']} benchmarks)",
            )
            for s in available_suites
        ]
        benchmark_suite = inquirer.fuzzy(
            message="Benchmark suite:",
            choices=suite_choices,
            long_instruction="Type to filter. Each suite is a pre-defined list of benchmarks",
        ).execute()
    elif use_suite and not available_suites:
        print(f"  No benchmark suites found in {suites_root}. Enter manually.")  # noqa: T201
        use_suite = False

    if not use_suite:
        raw = inquirer.text(
            message="Benchmarks (comma-separated):",
            long_instruction="e.g. afc-curl-delta-05, afc-libxml2-delta-03",
            validate=lambda val: len(val.strip()) > 0,
            invalid_message="At least one benchmark required",
        ).execute()
        benchmarks = [b.strip() for b in raw.split(",") if b.strip()]

    sanitizers = inquirer.checkbox(
        message="Sanitizers:",
        choices=_SANITIZER_CHOICES,
        long_instruction="Each sanitizer creates separate trials. Space to toggle, Enter to confirm",
        validate=lambda val: len(val) > 0,
        invalid_message="Select at least one sanitizer",
    ).execute()

    # --- CRS Compose section ---
    print("\n-- CRS Configuration --")  # noqa: T201
    available_crs = discover_crs_registry(registry_dir)

    # Filter CRS entries to those matching the selected task type.
    # Task values are "bugfinding"/"bugfixing"; registry uses "bug-finding"/"bug-fixing".
    task_type_prefix = "bug-finding" if task == "bugfinding" else "bug-fixing"
    compatible_crs = [
        entry
        for entry in available_crs
        if any(t.startswith(task_type_prefix) for t in entry["types"])
    ]

    selected_crs_names: List[str] = []
    if compatible_crs:
        crs_choices: list[Any] = [
            Choice(
                value=entry["name"],
                name=f"{entry['name']:40s} [{', '.join(entry['types']) or 'unknown'}]",
            )
            for entry in compatible_crs
        ]
        selected_crs_names = inquirer.checkbox(
            message="Select CRS:",
            choices=crs_choices,
            long_instruction="Space to toggle, Enter to confirm. Resource config follows",
            validate=lambda val: len(val) > 0,
            invalid_message="Select at least one CRS",
        ).execute()

        if inquirer.confirm(
            message="Add a custom CRS not in the registry?", default=False
        ).execute():
            custom = inquirer.text(
                message="Custom CRS names (comma-separated):",
                validate=lambda val: len(val.strip()) > 0,
                invalid_message="Enter at least one name",
            ).execute()
            selected_crs_names.extend(c.strip() for c in custom.split(",") if c.strip())
    elif available_crs:
        print(  # noqa: T201
            f"  No registered CRS supports '{task_type_prefix}'. Enter manually."
        )
        raw_crs = inquirer.text(
            message="CRS names (comma-separated):",
            validate=lambda val: len(val.strip()) > 0,
            invalid_message="At least one CRS is required",
        ).execute()
        selected_crs_names = [c.strip() for c in raw_crs.split(",") if c.strip()]
    else:
        raw_crs = inquirer.text(
            message="CRS names (comma-separated):",
            validate=lambda val: len(val.strip()) > 0,
            invalid_message="At least one CRS is required",
        ).execute()
        selected_crs_names = [c.strip() for c in raw_crs.split(",") if c.strip()]

    # Per-CRS resource configuration
    # Carry forward the previous CRS's values as defaults for the next one.
    default_cores = "8"
    default_mem = ""
    crs_services: Dict[str, Any] = {}
    for crs_name in selected_crs_names:
        print(f"\n  -- {crs_name} --")  # noqa: T201
        cores_raw = inquirer.text(
            message=f"  CPU cores for '{crs_name}':",
            default=default_cores,
            long_instruction="Number of CPU cores allocated to this CRS container",
            validate=NumberValidator(float_allowed=False),
            invalid_message="Enter a positive integer",
        ).execute()
        num_cores = int(cores_raw)
        default_cores = str(num_cores)

        mem_limit = inquirer.text(
            message=f"  Memory limit for '{crs_name}':",
            default=default_mem,
            long_instruction="Docker memory limit (e.g. 8G, 16G). Leave empty for unlimited",
        ).execute()
        default_mem = mem_limit.strip()

        service_config: Dict[str, Any] = {"num_cores": num_cores}
        if mem_limit.strip():
            service_config["mem_limit"] = mem_limit.strip()
        crs_services[crs_name] = service_config

    # --- Runtime section ---
    print("\n-- Runtime --")  # noqa: T201

    trials = int(
        inquirer.text(
            message="Number of trials:",
            default="1",
            long_instruction="How many times to repeat the full experiment for statistical significance",
            validate=NumberValidator(float_allowed=False),
        ).execute()
    )
    max_total_time = _prompt_duration("Max total time per trial:", default="8h")
    build_timeout = _prompt_duration("Build timeout:", default="1h")
    run_timeout = _prompt_duration("Run timeout:", default="4h")
    verify_timeout = _prompt_duration("Verify timeout:", default="2h")

    pov_early_stop = False
    if task == "bugfinding":
        pov_early_stop = inquirer.confirm(
            message="Enable POV early stop?",
            default=False,
            long_instruction="Terminate trial early when all CPVs for a harness are confirmed",
        ).execute()

    # --- LiteLLM section ---
    print("\n-- LLM Configuration --")  # noqa: T201

    litellm_selection = inquirer.select(
        message="LiteLLM mode:",
        choices=_LITELLM_MODE_CHOICES,
        default="external",
        long_instruction="LLM-based CRS need 'external'. Pure fuzzers can 'skip'",
    ).execute()

    skip_litellm = litellm_selection == "skip"
    litellm_mode: Optional[str] = None if skip_litellm else litellm_selection
    llm_tracking_enabled = True
    litellm_cost_budget: Optional[float] = None

    if not skip_litellm:
        llm_tracking_enabled = inquirer.confirm(
            message="Enable LLM usage tracking?",
            default=True,
            long_instruction="Track per-trial cost and token metrics via LiteLLM Virtual Keys",
        ).execute()

        if inquirer.confirm(
            message="Set a per-trial LLM cost budget?",
            default=False,
            long_instruction="When budget is exceeded, the CRS LLM key is revoked",
        ).execute():
            budget_str = inquirer.text(
                message="Cost budget (USD):",
                default="10.0",
                long_instruction="Maximum LLM spend in USD per trial",
                validate=lambda val: _is_positive_float(val),
                invalid_message="Enter a positive number",
            ).execute()
            litellm_cost_budget = float(budget_str)

    # --- Storage section ---
    print("\n-- Storage --")  # noqa: T201

    experiment_filestore = inquirer.text(
        message="Experiment filestore path:",
        default="./experiment-data",
        long_instruction="Directory for trial outputs, logs, and raw data",
    ).execute()

    report_filestore = inquirer.text(
        message="Report filestore path:",
        default="./report-data",
        long_instruction="Directory for HTML reports and summary data",
    ).execute()

    # --- Distributed / Parallel section ---
    print("\n-- Distributed / Parallel Processing --")  # noqa: T201

    use_distributed = inquirer.confirm(
        message="Enable distributed mode?",
        default=False,
        long_instruction="Run workers/evaluators on multiple machines via Redis job queue",
    ).execute()

    redis_host: Optional[str] = None
    if use_distributed:
        redis_host = inquirer.text(
            message="Redis host:",
            default="localhost:6379",
            long_instruction="hostname:port for the Redis job queue server",
        ).execute()

    worker_jobs = int(
        inquirer.text(
            message="Worker parallel jobs:",
            default="1",
            long_instruction="Concurrent trial executions per worker process",
            validate=NumberValidator(float_allowed=False),
        ).execute()
    )
    worker_cores = int(
        inquirer.text(
            message="Worker cores per job:",
            default="8",
            long_instruction="CPU cores allocated to each parallel trial",
            validate=NumberValidator(float_allowed=False),
        ).execute()
    )
    worker_config = {"jobs": worker_jobs, "cores_per_job": worker_cores}

    evaluator_jobs = int(
        inquirer.text(
            message="Evaluator parallel jobs:",
            default="4",
            long_instruction="Concurrent build/verify tasks for POV evaluation",
            validate=NumberValidator(float_allowed=False),
        ).execute()
    )
    evaluator_cores = int(
        inquirer.text(
            message="Evaluator cores per job:",
            default="4",
            long_instruction="CPU cores allocated to each evaluator build/verify task",
            validate=NumberValidator(float_allowed=False),
        ).execute()
    )
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


def _is_positive_float(val: str) -> bool:
    """Check if value is a positive float."""
    try:
        return float(val) > 0
    except ValueError:
        return False


def _validate_duration(val: str) -> bool:
    """Check if value is a valid duration (e.g. 1h, 30m, 3600)."""
    try:
        return int(parse_timespan(val)) >= 1
    except Exception:  # noqa: BLE001
        return False


def _prompt_duration(message: str, *, default: str) -> int:
    """Prompt for a duration value, accepting human-friendly formats.

    Accepts formats like: 1h, 30m, 4h, 90s, 3600, or '1 hour'.

    Returns:
        Duration in seconds as int.
    """
    raw = inquirer.text(
        message=message,
        default=default,
        long_instruction="Formats: 1h, 30m, 2h, 90s, or plain seconds (3600)",
        validate=_validate_duration,
        invalid_message="Invalid duration. Try: 1h, 30m, 8h, 3600",
    ).execute()
    return int(parse_timespan(raw))


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
            print("\nValidation errors in generated config:")  # noqa: T201
            for error in result.errors:
                print(f"  - {error.message}")  # noqa: T201
            print("\nConfig was NOT written. Fix the issues and try again.")  # noqa: T201
            return False
        print("\nGenerated config passed validation.")  # noqa: T201

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml_content, encoding="utf-8")
    print(f"\nConfig written to: {output_path}")  # noqa: T201
    return True
