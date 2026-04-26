from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from crsbench.genconfig_tui.core import SECTION_ORDER

FieldKind = Literal["text", "int", "bool", "select", "multiselect"]
VisibilityRule = Callable[[dict[str, Any]], bool]


@dataclass(frozen=True)
class FieldOption:
    label: str
    value: Any


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    kind: FieldKind
    default: Any = None
    placeholder: str = ""
    help_text: str = ""
    options: tuple[FieldOption, ...] = ()
    visible_when: VisibilityRule | None = None

    def is_visible(self, section_state: dict[str, Any]) -> bool:
        if self.visible_when is None:
            return True
        return self.visible_when(section_state)


@dataclass(frozen=True)
class SectionSpec:
    key: str
    title: str
    description: str
    fields: tuple[FieldSpec, ...] = field(default_factory=tuple)


def _when_true(key: str) -> VisibilityRule:
    return lambda state: bool(state.get(key))


def _when_false(key: str) -> VisibilityRule:
    return lambda state: not bool(state.get(key))


SECTION_SPECS: dict[str, SectionSpec] = {
    "experiment": SectionSpec(
        key="experiment",
        title="Experiment",
        description="Identity, task type, benchmark targeting, and evaluation mode.",
        fields=(
            FieldSpec("name", "Experiment name", "text", "gce-sanity-dynamic"),
            FieldSpec(
                "task",
                "Task",
                "select",
                "bugfinding",
                options=(
                    FieldOption("Bugfinding", "bugfinding"),
                    FieldOption("Bugfixing", "bugfixing"),
                ),
            ),
            FieldSpec("benchmark_suite", "Benchmark suite", "text", "sanity"),
            FieldSpec(
                "benchmarks",
                "Specific benchmarks",
                "text",
                "",
                placeholder="optional comma-separated benchmark ids",
            ),
            FieldSpec(
                "mode",
                "Mode",
                "select",
                "delta",
                options=(
                    FieldOption("Delta", "delta"),
                    FieldOption("Full", "full"),
                    FieldOption("All", "all"),
                    FieldOption("Auto", "auto"),
                ),
            ),
            FieldSpec(
                "sanitizers",
                "Sanitizers",
                "multiselect",
                ["address"],
                options=(
                    FieldOption("Address", "address"),
                    FieldOption("Memory", "memory"),
                    FieldOption("Undefined", "undefined"),
                ),
            ),
            FieldSpec(
                "only_cpv_harnesses",
                "Only CPV harnesses",
                "bool",
                default=True,
            ),
        ),
    ),
    "runtime": SectionSpec(
        key="runtime",
        title="Runtime",
        description="Trial counts, timeout budget, Redis, LiteLLM, and explicit runtime inputs.",
        fields=(
            FieldSpec("trials", "Trials", "int", 1),
            FieldSpec(
                "interleave_crs_enqueue",
                "Round-robin CRS enqueue",
                "bool",
                default=True,
            ),
            FieldSpec("max_total_time", "Max total time (sec)", "int", 7201),
            FieldSpec("build_timeout", "Build timeout (sec)", "int", 3600),
            FieldSpec("run_timeout", "Run timeout (sec)", "int", 600),
            FieldSpec("verify_timeout", "Verify timeout (sec)", "int", 600),
            FieldSpec("redis_host", "Redis host", "text", "redis-server:6379"),
            FieldSpec("skip_litellm", "Skip LiteLLM", "bool", default=True),
            FieldSpec(
                "litellm_mode",
                "LiteLLM mode",
                "select",
                "external",
                options=(FieldOption("External", "external"),),
                visible_when=_when_false("skip_litellm"),
            ),
            FieldSpec(
                "llm_tracking_enabled",
                "LLM tracking enabled",
                "bool",
                default=True,
                visible_when=_when_false("skip_litellm"),
            ),
            FieldSpec("pov_enabled", "Enable POV inputs", "bool", default=False),
            FieldSpec(
                "pov_max_variants_per_cpv",
                "POV max variants per CPV",
                "int",
                1,
                visible_when=_when_true("pov_enabled"),
            ),
            FieldSpec("sarif_enabled", "Enable SARIF hints", "bool", default=False),
            FieldSpec(
                "sarif_level",
                "SARIF level",
                "int",
                1,
                visible_when=_when_true("sarif_enabled"),
            ),
            FieldSpec("seed_enabled", "Enable seed inputs", "bool", default=False),
            FieldSpec(
                "seed_max_time",
                "Seed max time (sec)",
                "int",
                600,
                visible_when=_when_true("seed_enabled"),
            ),
            FieldSpec("diff_enabled", "Enable diff inputs", "bool", default=False),
            FieldSpec("snapshot_period", "Snapshot period (sec)", "int", 900),
        ),
    ),
    "storage": SectionSpec(
        key="storage",
        title="Storage",
        description="Output locations and result-copy behavior.",
        fields=(
            FieldSpec(
                "experiment_filestore",
                "Experiment filestore",
                "text",
                "/tmp/crsbench/experiment-data",
            ),
            FieldSpec(
                "report_filestore",
                "Report filestore",
                "text",
                "/tmp/crsbench/report-data",
            ),
            FieldSpec("keep_only_results", "Keep only results", "bool", default=False),
            FieldSpec(
                "cleanup_after_trial",
                "Cleanup after trial",
                "bool",
                default=False,
            ),
            FieldSpec(
                "copy_results_after_trial",
                "Copy results after trial",
                "bool",
                default=False,
            ),
            FieldSpec(
                "results_filestore",
                "Results filestore",
                "text",
                "",
                visible_when=_when_true("copy_results_after_trial"),
            ),
        ),
    ),
    "resources": SectionSpec(
        key="resources",
        title="Resources",
        description="Per-trial CPU and memory requests.",
        fields=(
            FieldSpec("cores_per_trial", "Cores per trial", "int", 2),
            FieldSpec("memory_per_trial", "Memory per trial", "text", "4G"),
            FieldSpec("cpu_tag", "CPU tag", "text", ""),
        ),
    ),
    "worker": SectionSpec(
        key="worker",
        title="Worker",
        description="Distributed worker job settings.",
        fields=(
            FieldSpec("jobs", "Worker jobs", "int", 1),
            FieldSpec("cores_per_job", "Worker cores per job", "int", 2),
            FieldSpec("continuous", "Continuous mode", "bool", default=True),
            FieldSpec("minimum_disk_size", "Minimum disk size", "text", "10GB"),
        ),
    ),
    "evaluator": SectionSpec(
        key="evaluator",
        title="Evaluator",
        description="Build and verification job settings.",
        fields=(
            FieldSpec("jobs", "Evaluator jobs", "int", 1),
            FieldSpec("cores_per_job", "Evaluator cores per job", "int", 4),
            FieldSpec("idle_timeout", "Idle timeout (sec)", "int", 0),
        ),
    ),
    "crs_compose": SectionSpec(
        key="crs_compose",
        title="CRS Compose",
        description="Compose service selection and shared infra behavior.",
        fields=(
            FieldSpec("service_name", "CRS service name", "text", "crs-libfuzzer"),
            FieldSpec("service_num_cores", "CRS service cores", "int", 2),
            FieldSpec("service_mem_limit", "CRS service mem limit", "text", ""),
            FieldSpec("infra_shared", "Share infra CPU pool", "bool", default=True),
            FieldSpec(
                "infra_num_cores",
                "Infra dedicated cores",
                "int",
                1,
                visible_when=_when_false("infra_shared"),
            ),
            FieldSpec("infra_mem_limit", "Infra mem limit", "text", ""),
            FieldSpec("oss_crs_cmd", "oss-crs command", "text", ""),
            FieldSpec("work_dir", "Work dir", "text", ""),
        ),
    ),
    "cloud": SectionSpec(
        key="cloud",
        title="Cloud",
        description="Optional GCE-backed launch settings for orchestrator, workers, and evaluators.",
        fields=(
            FieldSpec("enabled", "Enable cloud mode", "bool", default=False),
            FieldSpec(
                "provider_project",
                "GCE project",
                "text",
                "aixcc-426805",
                visible_when=_when_true("enabled"),
            ),
            FieldSpec(
                "provider_network",
                "GCE network",
                "text",
                "",
                visible_when=_when_true("enabled"),
            ),
            FieldSpec(
                "provider_subnetwork",
                "GCE subnetwork",
                "text",
                "",
                visible_when=_when_true("enabled"),
            ),
            FieldSpec(
                "provider_region",
                "GCE region",
                "text",
                "us-east5",
                visible_when=lambda state: bool(state.get("enabled"))
                and not bool(state.get("provider_regions")),
            ),
            FieldSpec(
                "provider_fallback",
                "Fallback across regions",
                "bool",
                default=True,
                visible_when=lambda state: bool(state.get("enabled"))
                and bool(state.get("provider_regions")),
            ),
            FieldSpec(
                "provider_zones",
                "GCE zones",
                "text",
                "",
                placeholder="optional comma-separated zones",
                visible_when=_when_true("enabled"),
            ),
            FieldSpec(
                "provider_ssh_via_iap",
                "SSH via IAP",
                "bool",
                default=True,
                visible_when=_when_true("enabled"),
            ),
            FieldSpec(
                "provider_assign_external_ip",
                "Assign external IP",
                "bool",
                default=True,
                visible_when=_when_true("enabled"),
            ),
            FieldSpec(
                "profile_machine_type",
                "Machine type",
                "text",
                "n2d-standard-16",
                visible_when=_when_true("enabled"),
            ),
            FieldSpec(
                "profile_boot_disk_size_gb",
                "Boot disk size (GiB)",
                "int",
                100,
                visible_when=_when_true("enabled"),
            ),
            FieldSpec(
                "profile_boot_disk_type",
                "Boot disk type",
                "text",
                "",
                placeholder="optional, e.g. pd-ssd or pd-balanced",
                visible_when=_when_true("enabled"),
            ),
            FieldSpec(
                "profile_image",
                "Base image",
                "text",
                "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
                visible_when=_when_true("enabled"),
            ),
            FieldSpec(
                "profile_service_account_email",
                "Service account email",
                "text",
                "153298433405-compute@developer.gserviceaccount.com",
                visible_when=_when_true("enabled"),
            ),
            FieldSpec(
                "profile_owner_label",
                "Owner label",
                "text",
                "yufu",
                visible_when=_when_true("enabled"),
            ),
            FieldSpec(
                "orchestrator_profile",
                "Orchestrator profile",
                "text",
                "gce-orchestrator-n2d",
                visible_when=_when_true("enabled"),
            ),
            FieldSpec(
                "orchestrator_region",
                "Orchestrator region",
                "text",
                "",
                visible_when=_when_true("enabled"),
            ),
            FieldSpec(
                "orchestrator_zones",
                "Orchestrator zones",
                "text",
                "",
                placeholder="optional comma-separated zones",
                visible_when=_when_true("enabled"),
            ),
            FieldSpec(
                "worker_profile",
                "Worker default profile",
                "text",
                "gce-worker-n2d",
                visible_when=_when_true("enabled"),
            ),
            FieldSpec(
                "evaluator_profile",
                "Evaluator default profile",
                "text",
                "gce-evaluator-n2d",
                visible_when=_when_true("enabled"),
            ),
            FieldSpec(
                "worker_count",
                "Worker count",
                "int",
                1,
                visible_when=_when_true("enabled"),
            ),
            FieldSpec(
                "evaluator_count",
                "Evaluator count",
                "int",
                1,
                visible_when=_when_true("enabled"),
            ),
            FieldSpec(
                "worker_region",
                "Worker region override",
                "text",
                "",
                visible_when=_when_true("enabled"),
            ),
            FieldSpec(
                "evaluator_region",
                "Evaluator region override",
                "text",
                "",
                visible_when=_when_true("enabled"),
            ),
            FieldSpec(
                "defaults_readiness_timeout_sec",
                "Readiness timeout (sec)",
                "int",
                1200,
                visible_when=_when_true("enabled"),
            ),
            FieldSpec(
                "defaults_crsbench_install_spec",
                "crsbench install spec",
                "text",
                "git+ssh://git@github.com/sslab-gatech/CRSBench.git",
                visible_when=_when_true("enabled"),
            ),
            FieldSpec(
                "defaults_crsbench_git_ref",
                "crsbench git ref",
                "text",
                "main",
                visible_when=_when_true("enabled"),
            ),
            FieldSpec(
                "defaults_github_deploy_key_path",
                "GitHub deploy key path",
                "text",
                ".crsbench-keys/crsbench-deploy",
                visible_when=_when_true("enabled"),
            ),
            FieldSpec(
                "bootstrap_prepare_mode",
                "Prepare mode",
                "select",
                "full",
                options=(
                    FieldOption("Full", "full"),
                    FieldOption("Skip base images", "skip_base_images"),
                ),
                visible_when=_when_true("enabled"),
            ),
            FieldSpec(
                "bootstrap_download_benchmarks",
                "Download benchmarks",
                "select",
                "auto",
                options=(
                    FieldOption("Auto", "auto"),
                    FieldOption("Always", "always"),
                    FieldOption("Never", "never"),
                ),
                visible_when=_when_true("enabled"),
            ),
            FieldSpec(
                "bootstrap_gitcache",
                "Enable gitcache wrapper",
                "bool",
                default=False,
                visible_when=_when_true("enabled"),
            ),
            FieldSpec(
                "env_CRSBENCH_LLM_UPSTREAM_BASE_URL",
                "LLM upstream base URL env ref",
                "text",
                "os.environ/CRSBENCH_LLM_UPSTREAM_BASE_URL",
                visible_when=_when_true("enabled"),
            ),
            FieldSpec(
                "env_CRSBENCH_LLM_MASTER_KEY",
                "LLM master key env ref",
                "text",
                "os.environ/CRSBENCH_LLM_MASTER_KEY",
                visible_when=_when_true("enabled"),
            ),
            FieldSpec(
                "env_HF_TOKEN",
                "HF token env ref",
                "text",
                "os.environ/HF_TOKEN",
                visible_when=_when_true("enabled"),
            ),
        ),
    ),
}


def default_form_state() -> dict[str, dict[str, Any]]:
    state: dict[str, dict[str, Any]] = {}
    for section in SECTION_ORDER:
        section_spec = SECTION_SPECS[section]
        state[section] = {
            field.key: deepcopy(field.default)
            for field in section_spec.fields
            if field.default is not None
        }
    return state
