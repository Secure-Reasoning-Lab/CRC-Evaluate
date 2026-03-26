from datetime import datetime
from pathlib import Path

from crsbench.genconfig_tui.core import (
    build_grouped_config,
    dump_yaml,
    load_state_from_grouped_config,
    make_output_path,
    write_grouped_config,
)
from crsbench.genconfig_tui.schema_bridge import validate_grouped_config


def test_build_grouped_config_omits_empty_optional_sections():
    state = {
        "experiment": {
            "name": "demo-exp",
            "task": "bugfixing",
            "benchmark_suite": "sanity",
            "mode": "delta",
        },
        "runtime": {
            "trials": 1,
            "max_total_time": 4001,
            "build_timeout": 1200,
            "run_timeout": 600,
            "verify_timeout": 600,
            "redis_host": "redis-server:6379",
            "skip_litellm": True,
            "pov_enabled": True,
            "pov_max_variants_per_cpv": 1,
        },
        "storage": {
            "experiment_filestore": "/tmp/exp",
            "report_filestore": "/tmp/report",
        },
        "resources": {},
        "worker": {},
        "evaluator": {},
        "crs_compose": {
            "service_name": "crs-libfuzzer",
            "service_num_cores": 2,
            "infra_shared": True,
        },
        "cloud": {},
    }

    grouped = build_grouped_config(state)

    assert grouped["experiment"] == {
        "name": "demo-exp",
        "task": "bugfixing",
        "benchmark_suite": "sanity",
        "mode": "delta",
    }
    assert grouped["runtime"]["inputs"]["pov"] == {
        "enabled": True,
        "max_variants_per_cpv": 1,
    }
    assert grouped["crs_compose"]["oss_crs_infra"] == {"shared": True}
    assert grouped["crs_compose"]["crs-libfuzzer"] == {"num_cores": 2}
    assert "worker" not in grouped
    assert "cloud" not in grouped


def test_dump_yaml_keeps_grouped_structure():
    yaml_text = dump_yaml(
        {
            "experiment": {"name": "demo-exp"},
            "runtime": {"trials": 1},
        }
    )

    assert "experiment:\n" in yaml_text
    assert "name: demo-exp" in yaml_text
    assert "runtime:\n" in yaml_text


def test_make_output_path_uses_timestamped_yaml_name():
    path = make_output_path(
        output_dir=Path("/tmp"),
        prefix="gce-mgf-dynamic",
        now=datetime(2026, 3, 25, 17, 49, 6),
    )

    assert path == Path("/tmp/gce-mgf-dynamic-20260325-174906.yaml")


def test_load_state_from_grouped_config_flattens_known_sections():
    grouped = {
        "experiment": {
            "name": "loaded-exp",
            "task": "bugfinding",
            "benchmark_suite": "sanity",
            "mode": "delta",
        },
        "runtime": {
            "trials": 2,
            "max_total_time": 7201,
            "build_timeout": 3600,
            "run_timeout": 600,
            "verify_timeout": 600,
            "inputs": {
                "pov": {"enabled": False, "max_variants_per_cpv": 1},
            },
        },
        "storage": {
            "experiment_filestore": "/tmp/exp",
            "report_filestore": "/tmp/report",
        },
        "crs_compose": {
            "oss_crs_infra": {"shared": True},
            "crs-libfuzzer": {"num_cores": 2},
        },
    }

    state, extras = load_state_from_grouped_config(grouped)

    assert state["experiment"]["name"] == "loaded-exp"
    assert state["runtime"]["pov_enabled"] is False
    assert state["runtime"]["pov_max_variants_per_cpv"] == 1
    assert state["crs_compose"]["service_name"] == "crs-libfuzzer"
    assert state["crs_compose"]["service_num_cores"] == 2
    assert extras == {}


def test_load_state_from_grouped_config_round_trips_nested_litellm_block():
    grouped = {
        "experiment": {
            "name": "loaded-exp",
            "task": "bugfinding",
            "benchmark_suite": "sanity",
            "mode": "delta",
        },
        "runtime": {
            "trials": 2,
            "max_total_time": 7201,
            "build_timeout": 3600,
            "run_timeout": 600,
            "verify_timeout": 600,
            "litellm": {
                "mode": "external",
                "tracking_enabled": True,
                "cost_budget": 10,
                "skip": False,
            },
        },
        "storage": {
            "experiment_filestore": "/tmp/exp",
            "report_filestore": "/tmp/report",
        },
        "crs_compose": {
            "oss_crs_infra": {"shared": True},
            "crs-libfuzzer": {"num_cores": 2},
        },
    }

    state, extras = load_state_from_grouped_config(grouped)

    assert state["runtime"]["litellm_mode"] == "external"
    assert state["runtime"]["llm_tracking_enabled"] is True
    assert state["runtime"]["litellm_cost_budget"] == 10
    assert state["runtime"]["skip_litellm"] is False

    rebuilt = build_grouped_config(state, section_extras=extras)

    assert rebuilt["runtime"]["litellm"] == {
        "mode": "external",
        "tracking_enabled": True,
        "cost_budget": 10,
        "skip": False,
    }
    assert "litellm_mode" not in rebuilt["runtime"]
    assert "llm_tracking_enabled" not in rebuilt["runtime"]
    assert "skip_litellm" not in rebuilt["runtime"]


def test_load_state_from_grouped_config_maps_cloud_fields_and_preserves_unknowns():
    grouped = {
        "experiment": {
            "name": "loaded-exp",
            "task": "bugfinding",
            "benchmark_suite": "sanity",
            "mode": "delta",
        },
        "runtime": {
            "trials": 2,
            "max_total_time": 7201,
            "build_timeout": 3600,
            "run_timeout": 600,
            "verify_timeout": 600,
        },
        "storage": {
            "experiment_filestore": "/tmp/exp",
            "report_filestore": "/tmp/report",
        },
        "crs_compose": {
            "oss_crs_infra": {"shared": True},
            "crs-libfuzzer": {"num_cores": 2},
        },
        "cloud": {
            "providers": {
                "gce": {
                    "project": "demo-project",
                    "region": "us-east5",
                    "ssh_via_iap": True,
                    "profile_defaults": {
                        "machine_type": "n2d-standard-16",
                        "boot_disk_size_gb": 100,
                        "image": "ubuntu-image",
                        "service_account_email": "svc@example.com",
                        "owner_label": "owner",
                    },
                }
            },
            "workers": {
                "defaults": {"count": 2},
                "placements": [{"region": "us-east1"}],
            },
            "custom_block": {"keep": "me"},
        },
    }

    state, extras = load_state_from_grouped_config(grouped)

    assert state["cloud"]["provider_project"] == "demo-project"
    assert state["cloud"]["provider_region"] == "us-east5"
    assert state["cloud"]["provider_ssh_via_iap"] is True
    assert state["cloud"]["profile_machine_type"] == "n2d-standard-16"
    assert state["cloud"]["worker_count"] == 2
    assert state["cloud"]["worker_region"] == "us-east1"
    assert extras == {"cloud": {"custom_block": {"keep": "me"}}}


def test_build_grouped_config_preserves_section_extras():
    state = {
        "experiment": {
            "name": "demo-exp",
            "task": "bugfixing",
            "benchmark_suite": "sanity",
            "mode": "delta",
        },
        "runtime": {
            "trials": 1,
            "max_total_time": 4001,
            "build_timeout": 1200,
            "run_timeout": 600,
            "verify_timeout": 600,
            "skip_litellm": True,
            "pov_enabled": True,
            "pov_max_variants_per_cpv": 1,
        },
        "storage": {
            "experiment_filestore": "/tmp/exp",
            "report_filestore": "/tmp/report",
        },
        "crs_compose": {
            "service_name": "crs-libfuzzer",
            "service_num_cores": 2,
            "infra_shared": True,
        },
    }

    grouped = build_grouped_config(
        state,
        section_extras={"runtime": {"custom_runtime": {"preserve": True}}},
    )

    assert grouped["runtime"]["custom_runtime"] == {"preserve": True}


def test_cloud_round_trip_preserves_empty_inherited_placements():
    grouped = {
        "experiment": {
            "name": "loaded-exp",
            "task": "bugfinding",
            "benchmark_suite": "sanity",
            "mode": "delta",
        },
        "runtime": {
            "trials": 1,
            "max_total_time": 7201,
            "build_timeout": 3600,
            "run_timeout": 600,
            "verify_timeout": 600,
        },
        "storage": {
            "experiment_filestore": "/tmp/exp",
            "report_filestore": "/tmp/report",
        },
        "crs_compose": {
            "oss_crs_infra": {"shared": True},
            "crs-libfuzzer": {"num_cores": 2},
        },
        "cloud": {
            "providers": {
                "gce": {
                    "project": "demo-project",
                    "regions": ["us-east5", "us-east1"],
                    "fallback": True,
                    "profile_defaults": {
                        "machine_type": "n2d-standard-16",
                        "boot_disk_size_gb": 100,
                        "image": "ubuntu-image",
                        "service_account_email": "svc@example.com",
                        "owner_label": "owner",
                    },
                    "instance_profiles": {
                        "gce-orchestrator-n2d": {},
                        "gce-worker-n2d": {},
                        "gce-evaluator-n2d": {},
                    },
                }
            },
            "orchestrator": {"instance_profile": "gce-orchestrator-n2d"},
            "workers": {
                "defaults": {"instance_profile": "gce-worker-n2d", "count": 1},
                "placements": [{}, {}],
            },
            "evaluators": {
                "defaults": {"instance_profile": "gce-evaluator-n2d", "count": 1},
                "placements": [{}],
            },
        },
    }

    state, extras = load_state_from_grouped_config(grouped)
    rebuilt = build_grouped_config(state, section_extras=extras)

    assert rebuilt["cloud"]["providers"]["gce"]["regions"] == ["us-east5", "us-east1"]
    assert rebuilt["cloud"]["workers"]["placements"] == [{}, {}]
    assert rebuilt["cloud"]["evaluators"]["placements"] == [{}]
    validate_grouped_config(rebuilt)


def test_build_grouped_config_uses_exactly_one_crs_infra_cpu_mode():
    grouped = build_grouped_config(
        {
            "crs_compose": {
                "infra_shared": False,
                "infra_num_cores": 4,
                "infra_mem_limit": "8G",
                "service_name": "crs-libfuzzer",
            },
        }
    )

    assert grouped["crs_compose"]["oss_crs_infra"] == {
        "num_cores": 4,
        "mem_limit": "8G",
    }


def test_write_grouped_config_writes_timestamped_yaml(tmp_path):
    path = write_grouped_config(
        {"experiment": {"name": "demo-exp"}},
        output_dir=tmp_path,
        prefix="config",
        now=datetime(2026, 3, 25, 17, 49, 6),
    )

    assert path == tmp_path / "config-20260325-174906.yaml"
    assert path.read_text(encoding="utf-8").strip() == "experiment:\n  name: demo-exp"


def test_write_grouped_config_round_trips_loaded_yaml_comments(tmp_path):
    source = tmp_path / "source.yaml"
    original_source = (
        "# top comment\n"
        "experiment:\n"
        "  # keep me\n"
        "  name: old-name\n"
    )
    source.write_text(original_source, encoding="utf-8")

    grouped = {"experiment": {"name": "new-name"}}

    written_path = write_grouped_config(
        grouped,
        output_path=tmp_path / "copy.yaml",
        source_roundtrip_path=source,
    )

    written = (tmp_path / "copy.yaml").read_text(encoding="utf-8")
    assert written_path == tmp_path / "copy.yaml"
    assert "# top comment" in written
    assert "# keep me" in written
    assert "name: new-name" in written
    assert "old-name" not in written
    assert source.read_text(encoding="utf-8") == original_source
