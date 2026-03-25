from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import yaml

SECTION_ORDER = (
    "experiment",
    "runtime",
    "storage",
    "resources",
    "worker",
    "evaluator",
    "crs_compose",
    "cloud",
)


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (dict, list, tuple, set)):
        return len(value) == 0
    return False


def _prune(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            pruned = _prune(item)
            if _is_blank(pruned):
                continue
            cleaned[key] = pruned
        return cleaned
    if isinstance(value, list):
        cleaned_list = [_prune(item) for item in value]
        return [item for item in cleaned_list if not _is_blank(item)]
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    return value


def _section(state: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = state.get(name, {})
    if not isinstance(value, Mapping):
        return {}
    return dict(value)


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = deepcopy(dict(base))
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _deep_difference(original: Any, known: Any) -> Any:
    if isinstance(original, Mapping) and isinstance(known, Mapping):
        diff: dict[str, Any] = {}
        for key, original_value in original.items():
            if key not in known:
                diff[key] = deepcopy(original_value)
                continue
            nested = _deep_difference(original_value, known[key])
            if not _is_blank(nested):
                diff[key] = nested
        return diff
    if original == known:
        return None
    return deepcopy(original)


def _pop_known(mapping: dict[str, Any], key: str) -> Any:
    return mapping.pop(key, None)


def _build_inputs(runtime_state: Mapping[str, Any]) -> dict[str, Any]:
    inputs: dict[str, Any] = {}
    for prefix in ("pov", "sarif", "seed", "diff"):
        enabled = runtime_state.get(f"{prefix}_enabled")
        nested: dict[str, Any] = {}
        if enabled is not None:
            nested["enabled"] = enabled
        for suffix in ("max_variants_per_cpv", "level", "max_time"):
            key = f"{prefix}_{suffix}"
            if key in runtime_state:
                nested[suffix] = runtime_state.get(key)
        if nested:
            inputs[prefix] = nested
    return _prune(inputs)


def _build_crs_compose_section(state: Mapping[str, Any]) -> dict[str, Any]:
    infra_shared = state.get("infra_shared")
    infra_num_cores = state.get("infra_num_cores")
    infra_mem_limit = state.get("infra_mem_limit")
    service_name = state.get("service_name")
    service_num_cores = state.get("service_num_cores")
    service_mem_limit = state.get("service_mem_limit")

    crs_compose: dict[str, Any] = {}
    if infra_shared:
        infra_payload = {
            "shared": True,
            "mem_limit": infra_mem_limit,
        }
    else:
        infra_payload = {
            "num_cores": infra_num_cores,
            "mem_limit": infra_mem_limit,
        }

    infra = _prune(infra_payload)
    if infra:
        crs_compose["oss_crs_infra"] = infra

    if service_name:
        crs_compose[str(service_name).strip()] = _prune(
            {
                "num_cores": service_num_cores,
                "mem_limit": service_mem_limit,
            }
        )

    for optional_key in ("oss_crs_cmd", "work_dir", "litellm_config_path"):
        if optional_key in state:
            crs_compose[optional_key] = state.get(optional_key)

    return _prune(crs_compose)


def _build_cloud_section(
    state: Mapping[str, Any],
    storage_state: Mapping[str, Any],
) -> dict[str, Any]:
    if not state or state.get("enabled") is False:
        return {}

    orchestrator_profile = state.get("orchestrator_profile") or "gce-orchestrator-n2d"
    worker_profile = state.get("worker_profile") or "gce-worker-n2d"
    evaluator_profile = state.get("evaluator_profile") or "gce-evaluator-n2d"
    worker_region = state.get("worker_region")
    evaluator_region = state.get("evaluator_region")

    worker_placement = _prune({"region": worker_region}) or {}
    evaluator_placement = _prune({"region": evaluator_region}) or {}

    env = {}
    for env_key in (
        "CRSBENCH_LLM_UPSTREAM_BASE_URL",
        "CRSBENCH_LLM_MASTER_KEY",
        "HF_TOKEN",
    ):
        value = state.get(f"env_{env_key}")
        if not _is_blank(value):
            env[env_key] = value

    return _prune(
        {
            "remote": {
                "experiment_root": state.get("remote_experiment_root")
                or storage_state.get("experiment_filestore")
            },
            "defaults": {
                "readiness_timeout_sec": state.get("defaults_readiness_timeout_sec"),
                "crsbench_install_spec": state.get("defaults_crsbench_install_spec"),
                "crsbench_git_ref": state.get("defaults_crsbench_git_ref"),
                "github_deploy_key_path": state.get("defaults_github_deploy_key_path"),
            },
            "env": env,
            "bootstrap": {
                "prepare_mode": state.get("bootstrap_prepare_mode"),
                "download_benchmarks": state.get("bootstrap_download_benchmarks"),
            },
            "providers": {
                "gce": {
                    "project": state.get("provider_project"),
                    "region": state.get("provider_region"),
                    "ssh_via_iap": state.get("provider_ssh_via_iap"),
                    "profile_defaults": {
                        "machine_type": state.get("profile_machine_type"),
                        "boot_disk_size_gb": state.get("profile_boot_disk_size_gb"),
                        "image": state.get("profile_image"),
                        "service_account_email": state.get(
                            "profile_service_account_email"
                        ),
                        "owner_label": state.get("profile_owner_label"),
                    },
                    "instance_profiles": {
                        orchestrator_profile: {},
                        worker_profile: {},
                        evaluator_profile: {},
                    },
                }
            },
            "orchestrator": {
                "instance_profile": orchestrator_profile,
            },
            "workers": {
                "defaults": {
                    "instance_profile": worker_profile,
                    "count": state.get("worker_count"),
                },
                "placements": [worker_placement],
            },
            "evaluators": {
                "defaults": {
                    "instance_profile": evaluator_profile,
                    "count": state.get("evaluator_count"),
                },
                "placements": [evaluator_placement],
            },
        }
    )


def build_grouped_config(
    state: Mapping[str, Any],
    section_extras: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    experiment_state = _section(state, "experiment")
    runtime_state = _section(state, "runtime")
    storage_state = _section(state, "storage")

    benchmarks = deepcopy(experiment_state.get("benchmarks"))
    if isinstance(benchmarks, str):
        benchmarks = [item.strip() for item in benchmarks.split(",") if item.strip()]

    grouped = {
        "experiment": {
            "name": experiment_state.get("name"),
            "task": experiment_state.get("task"),
            "benchmark_suite": experiment_state.get("benchmark_suite"),
            "benchmarks": benchmarks,
            "mode": experiment_state.get("mode"),
            "sanitizers": deepcopy(experiment_state.get("sanitizers")),
            "only_cpv_harnesses": experiment_state.get("only_cpv_harnesses"),
        },
        "runtime": {
            "trials": runtime_state.get("trials"),
            "max_total_time": runtime_state.get("max_total_time"),
            "build_timeout": runtime_state.get("build_timeout"),
            "run_timeout": runtime_state.get("run_timeout"),
            "verify_timeout": runtime_state.get("verify_timeout"),
            "per_pov_verify_timeout": runtime_state.get("per_pov_verify_timeout"),
            "redis_host": runtime_state.get("redis_host"),
            "skip_litellm": runtime_state.get("skip_litellm"),
            "litellm_mode": runtime_state.get("litellm_mode"),
            "llm_tracking_enabled": runtime_state.get("llm_tracking_enabled"),
            "patch_verify_variants": runtime_state.get("patch_verify_variants"),
            "snapshot_period": runtime_state.get("snapshot_period"),
            "pov_early_stop": runtime_state.get("pov_early_stop"),
            "coverage_enabled": runtime_state.get("coverage_enabled"),
            "coverage_saturation_time": runtime_state.get("coverage_saturation_time"),
            "coverage_early_stop": runtime_state.get("coverage_early_stop"),
            "inputs": _build_inputs(runtime_state),
        },
        "storage": {
            "experiment_filestore": storage_state.get("experiment_filestore"),
            "report_filestore": storage_state.get("report_filestore"),
            "keep_only_results": storage_state.get("keep_only_results"),
            "cleanup_after_trial": storage_state.get("cleanup_after_trial"),
            "copy_results_after_trial": storage_state.get("copy_results_after_trial"),
            "results_filestore": storage_state.get("results_filestore"),
        },
        "resources": _section(state, "resources"),
        "worker": _section(state, "worker"),
        "evaluator": _section(state, "evaluator"),
        "crs_compose": _build_crs_compose_section(_section(state, "crs_compose")),
        "cloud": _build_cloud_section(_section(state, "cloud"), storage_state),
    }
    pruned = _prune(grouped)

    if section_extras:
        merged_with_extras: dict[str, Any] = {}
        for section in pruned:
            section_value = pruned.get(section, {})
            extras_value = section_extras.get(section, {})
            if isinstance(section_value, Mapping) and isinstance(extras_value, Mapping):
                merged_with_extras[section] = _deep_merge(extras_value, section_value)
            else:
                merged_with_extras[section] = section_value
        return _prune(merged_with_extras)

    return pruned


def dump_yaml(data: Mapping[str, Any]) -> str:
    return yaml.safe_dump(
        _prune(dict(data)),
        sort_keys=False,
        default_flow_style=False,
    )


def dump_section_yaml(section_name: str, state: Mapping[str, Any]) -> str:
    grouped = build_grouped_config(state)
    section = grouped.get(section_name, {})
    return dump_yaml({section_name: section})


def load_state_from_grouped_config(
    grouped: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    grouped_dict = dict(grouped)
    extras: dict[str, dict[str, Any]] = {}

    experiment = deepcopy(dict(grouped_dict.get("experiment", {})))
    runtime = deepcopy(dict(grouped_dict.get("runtime", {})))
    storage = deepcopy(dict(grouped_dict.get("storage", {})))
    resources = deepcopy(dict(grouped_dict.get("resources", {})))
    worker = deepcopy(dict(grouped_dict.get("worker", {})))
    evaluator = deepcopy(dict(grouped_dict.get("evaluator", {})))
    crs_compose = deepcopy(dict(grouped_dict.get("crs_compose", {})))
    cloud = deepcopy(dict(grouped_dict.get("cloud", {})))

    inputs = deepcopy(dict(_pop_known(runtime, "inputs") or {}))
    pov = deepcopy(dict(_pop_known(inputs, "pov") or {}))
    sarif = deepcopy(dict(_pop_known(inputs, "sarif") or {}))
    seed = deepcopy(dict(_pop_known(inputs, "seed") or {}))
    diff = deepcopy(dict(_pop_known(inputs, "diff") or {}))

    service_name = ""
    service_values: dict[str, Any] = {}
    for key in list(crs_compose):
        value = crs_compose[key]
        if key in {"oss_crs_infra", "oss_crs_cmd", "work_dir", "litellm_config_path"}:
            continue
        if isinstance(value, Mapping):
            service_name = key
            service_values = dict(value)
            crs_compose.pop(key, None)
            break

    infra = deepcopy(dict(_pop_known(crs_compose, "oss_crs_infra") or {}))

    providers = deepcopy(dict(_pop_known(cloud, "providers") or {}))
    gce = deepcopy(dict(providers.get("gce", {})))
    profile_defaults = deepcopy(dict(gce.get("profile_defaults", {})))
    workers_cfg = deepcopy(dict(_pop_known(cloud, "workers") or {}))
    worker_defaults = deepcopy(dict(workers_cfg.get("defaults", {})))
    worker_placements = deepcopy(list(workers_cfg.get("placements", [])))
    first_worker_placement = (
        deepcopy(dict(worker_placements[0])) if worker_placements else {}
    )
    evaluators_cfg = deepcopy(dict(_pop_known(cloud, "evaluators") or {}))
    evaluator_defaults = deepcopy(dict(evaluators_cfg.get("defaults", {})))
    evaluator_placements = deepcopy(list(evaluators_cfg.get("placements", [])))
    first_evaluator_placement = (
        deepcopy(dict(evaluator_placements[0])) if evaluator_placements else {}
    )
    orchestrator_cfg = deepcopy(dict(_pop_known(cloud, "orchestrator") or {}))
    remote_cfg = deepcopy(dict(_pop_known(cloud, "remote") or {}))
    defaults_cfg = deepcopy(dict(_pop_known(cloud, "defaults") or {}))
    bootstrap_cfg = deepcopy(dict(_pop_known(cloud, "bootstrap") or {}))
    cloud_env = deepcopy(dict(_pop_known(cloud, "env") or {}))

    state = {
        "experiment": experiment,
        "runtime": _prune(
            {
                **{
                    key: runtime.get(key)
                    for key in (
                        "trials",
                        "max_total_time",
                        "build_timeout",
                        "run_timeout",
                        "verify_timeout",
                        "per_pov_verify_timeout",
                        "redis_host",
                        "skip_litellm",
                        "litellm_mode",
                        "llm_tracking_enabled",
                        "patch_verify_variants",
                        "snapshot_period",
                        "pov_early_stop",
                        "coverage_enabled",
                        "coverage_saturation_time",
                        "coverage_early_stop",
                    )
                },
                "pov_enabled": pov.get("enabled"),
                "pov_max_variants_per_cpv": pov.get("max_variants_per_cpv"),
                "sarif_enabled": sarif.get("enabled"),
                "sarif_level": sarif.get("level"),
                "seed_enabled": seed.get("enabled"),
                "seed_max_time": seed.get("max_time"),
                "diff_enabled": diff.get("enabled"),
            }
        ),
        "storage": storage,
        "resources": resources,
        "worker": worker,
        "evaluator": evaluator,
        "crs_compose": _prune(
            {
                "infra_shared": infra.get("shared"),
                "infra_num_cores": infra.get("num_cores"),
                "infra_mem_limit": infra.get("mem_limit"),
                "service_name": service_name,
                "service_num_cores": service_values.get("num_cores"),
                "service_mem_limit": service_values.get("mem_limit"),
                "oss_crs_cmd": _pop_known(crs_compose, "oss_crs_cmd"),
                "work_dir": _pop_known(crs_compose, "work_dir"),
                "litellm_config_path": _pop_known(crs_compose, "litellm_config_path"),
            }
        ),
        "cloud": _prune(
            {
                "enabled": bool(grouped_dict.get("cloud")),
                "remote_experiment_root": remote_cfg.get("experiment_root"),
                "defaults_readiness_timeout_sec": defaults_cfg.get(
                    "readiness_timeout_sec"
                ),
                "defaults_crsbench_install_spec": defaults_cfg.get(
                    "crsbench_install_spec"
                ),
                "defaults_crsbench_git_ref": defaults_cfg.get("crsbench_git_ref"),
                "defaults_github_deploy_key_path": defaults_cfg.get(
                    "github_deploy_key_path"
                ),
                "env_CRSBENCH_LLM_UPSTREAM_BASE_URL": cloud_env.get(
                    "CRSBENCH_LLM_UPSTREAM_BASE_URL"
                ),
                "env_CRSBENCH_LLM_MASTER_KEY": cloud_env.get("CRSBENCH_LLM_MASTER_KEY"),
                "env_HF_TOKEN": cloud_env.get("HF_TOKEN"),
                "bootstrap_prepare_mode": bootstrap_cfg.get("prepare_mode"),
                "bootstrap_download_benchmarks": bootstrap_cfg.get(
                    "download_benchmarks"
                ),
                "provider_project": gce.get("project"),
                "provider_region": gce.get("region"),
                "provider_ssh_via_iap": gce.get("ssh_via_iap"),
                "profile_machine_type": profile_defaults.get("machine_type"),
                "profile_boot_disk_size_gb": profile_defaults.get("boot_disk_size_gb"),
                "profile_image": profile_defaults.get("image"),
                "profile_service_account_email": profile_defaults.get(
                    "service_account_email"
                ),
                "profile_owner_label": profile_defaults.get("owner_label"),
                "orchestrator_profile": orchestrator_cfg.get("instance_profile"),
                "worker_profile": worker_defaults.get("instance_profile"),
                "evaluator_profile": evaluator_defaults.get("instance_profile"),
                "worker_count": worker_defaults.get("count"),
                "evaluator_count": evaluator_defaults.get("count"),
                "worker_region": first_worker_placement.get("region"),
                "evaluator_region": first_evaluator_placement.get("region"),
            }
        ),
    }

    generated_known = build_grouped_config(state)

    for section_name in SECTION_ORDER:
        section_value = _prune(
            _deep_difference(
                grouped_dict.get(section_name, {}),
                generated_known.get(section_name, {}),
            )
        )
        if section_value:
            extras[section_name] = section_value

    return state, extras


def read_grouped_config(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected mapping at top level in {path}")
    return loaded


def make_output_path(
    output_dir: Path = Path("/tmp"),
    prefix: str = "gce-mgf-dynamic",
    now: datetime | None = None,
) -> Path:
    current = now or datetime.now()
    filename = f"{prefix}-{current:%Y%m%d-%H%M%S}.yaml"
    return output_dir / filename


def write_grouped_config(
    data: Mapping[str, Any],
    output_dir: Path = Path("/tmp"),
    prefix: str = "gce-mgf-dynamic",
    now: datetime | None = None,
    output_path: Path | None = None,
) -> Path:
    path = output_path or make_output_path(
        output_dir=output_dir,
        prefix=prefix,
        now=now,
    )
    path.write_text(dump_yaml(data), encoding="utf-8")
    return path
