from datetime import datetime
from pathlib import Path

import crsbench.genconfig_tui.core as genconfig_core
import pytest
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
                        "boot_disk_type": "pd-balanced",
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
    assert state["cloud"]["profile_boot_disk_type"] == "pd-balanced"
    assert state["cloud"]["worker_count"] == 2
    assert state["cloud"]["worker_region"] == "us-east1"
    assert extras == {"cloud": {"custom_block": {"keep": "me"}}}


def test_load_state_from_grouped_config_does_not_enable_cloud_for_unknown_only_block():
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
            "custom_block": {"keep": "me"},
        },
    }

    state, extras = load_state_from_grouped_config(grouped)
    rebuilt = build_grouped_config(state, section_extras=extras)

    assert state["cloud"] == {"enabled": False}
    assert extras == {"cloud": {"custom_block": {"keep": "me"}}}
    assert rebuilt["cloud"] == {"custom_block": {"keep": "me"}}


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


def test_build_grouped_config_keeps_generated_cloud_instance_profiles():
    grouped = build_grouped_config(
        {
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
            "cloud": {
                "enabled": True,
                "provider_project": "aixcc-426805",
                "provider_region": "us-east5",
                "provider_ssh_via_iap": True,
                "profile_machine_type": "n2d-standard-16",
                "profile_boot_disk_size_gb": 100,
                "profile_boot_disk_type": "pd-balanced",
                "profile_image": "ubuntu-image",
                "profile_service_account_email": "svc@example.com",
                "profile_owner_label": "owner",
                "worker_count": 1,
                "evaluator_count": 1,
                "worker_region": "us-east5",
                "evaluator_region": "us-east5",
            },
        }
    )

    assert grouped["cloud"]["providers"]["gce"]["instance_profiles"] == {
        "gce-orchestrator-n2d": {},
        "gce-worker-n2d": {},
        "gce-evaluator-n2d": {},
    }
    validate_grouped_config(grouped)


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


def test_load_state_from_grouped_config_exposes_cloud_collection_state():
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
                    "regions": ["us-east5", "us-east1", "us-south1"],
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
                        "gce-worker-n2d": {"machine_type": "n2d-standard-32"},
                        "gce-evaluator-n2d": {},
                    },
                }
            },
            "orchestrator": {"instance_profile": "gce-orchestrator-n2d"},
            "workers": {
                "defaults": {"instance_profile": "gce-worker-n2d", "count": 1},
                "placements": [{}, {"region": "us-east1", "count": 2}],
            },
            "evaluators": {
                "defaults": {"instance_profile": "gce-evaluator-n2d", "count": 1},
                "placements": [{"zone": "us-east5-b"}],
            },
        },
    }

    state, extras = load_state_from_grouped_config(grouped)

    assert state["cloud"]["provider_regions"] == ["us-east5", "us-east1", "us-south1"]
    assert state["cloud"]["provider_fallback"] is True
    assert state["cloud"]["instance_profiles"] == [
        {"name": "gce-orchestrator-n2d"},
        {"name": "gce-worker-n2d", "machine_type": "n2d-standard-32"},
        {"name": "gce-evaluator-n2d"},
    ]
    assert state["cloud"]["worker_placements"] == [
        {},
        {"region": "us-east1", "count": 2},
    ]
    assert state["cloud"]["evaluator_placements"] == [{"zone": "us-east5-b"}]
    assert extras == {}


def test_build_grouped_config_prefers_cloud_collection_state():
    grouped = build_grouped_config(
        {
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
            "cloud": {
                "enabled": True,
                "provider_project": "demo-project",
                "provider_region": "legacy-region-should-not-win",
                "provider_regions": ["us-east5", "us-east1", "us-south1"],
                "provider_fallback": True,
                "provider_ssh_via_iap": True,
                "profile_machine_type": "n2d-standard-16",
                "profile_boot_disk_size_gb": 100,
                "profile_image": "ubuntu-image",
                "profile_service_account_email": "svc@example.com",
                "profile_owner_label": "owner",
                "orchestrator_profile": "gce-orchestrator-n2d",
                "worker_profile": "gce-worker-n2d",
                "evaluator_profile": "gce-evaluator-n2d",
                "worker_count": 1,
                "evaluator_count": 1,
                "instance_profiles": [
                    {"name": "gce-orchestrator-n2d"},
                    {"name": "gce-worker-n2d", "machine_type": "n2d-standard-32"},
                    {"name": "gce-evaluator-n2d"},
                ],
                "worker_placements": [{}, {"region": "us-east1", "count": 2}],
                "evaluator_placements": [{"zone": "us-east5-b"}],
            },
        }
    )

    assert grouped["cloud"]["providers"]["gce"]["regions"] == [
        "us-east5",
        "us-east1",
        "us-south1",
    ]
    assert grouped["cloud"]["providers"]["gce"]["fallback"] is True
    assert grouped["cloud"]["providers"]["gce"]["instance_profiles"] == {
        "gce-orchestrator-n2d": {},
        "gce-worker-n2d": {"machine_type": "n2d-standard-32"},
        "gce-evaluator-n2d": {},
    }
    assert grouped["cloud"]["workers"]["placements"] == [
        {},
        {"region": "us-east1", "count": 2},
    ]
    assert grouped["cloud"]["evaluators"]["placements"] == [{"zone": "us-east5-b"}]
    validate_grouped_config(grouped)


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
    original_source = "# top comment\nexperiment:\n  # keep me\n  name: old-name\n"
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


def test_round_trip_write_updates_loaded_file_preserves_comments_and_order(tmp_path):
    source = tmp_path / "source.yaml"
    source.write_text(
        "# top comment\n"
        "experiment:\n"
        "  # keep me\n"
        "  name: old-name\n"
        "runtime:\n"
        "  trials: 1\n",
        encoding="utf-8",
    )

    grouped = {
        "experiment": {"name": "new-name"},
        "runtime": {"trials": 2},
    }

    written_path = write_grouped_config(
        grouped,
        output_path=source,
        source_roundtrip_path=source,
    )

    written = source.read_text(encoding="utf-8")
    assert written_path == source
    assert "# top comment" in written
    assert "# keep me" in written
    assert written.index("experiment:") < written.index("runtime:")
    assert "name: new-name" in written
    assert "trials: 2" in written
    assert "old-name" not in written


def test_write_grouped_config_uses_in_memory_roundtrip_document_when_disk_changes(
    tmp_path,
):
    source = tmp_path / "source.yaml"
    source.write_text(
        "# loaded comment\nexperiment:\n  # keep loaded\n  name: old-name\n",
        encoding="utf-8",
    )
    loaded_document = genconfig_core.load_roundtrip_document(source)
    source.write_text(
        "# disk changed later\nexperiment:\n  # changed on disk\n  name: disk-name\n",
        encoding="utf-8",
    )

    write_grouped_config(
        {"experiment": {"name": "new-name"}},
        output_path=tmp_path / "copy.yaml",
        source_roundtrip_document=loaded_document,
    )

    written = (tmp_path / "copy.yaml").read_text(encoding="utf-8")
    assert "# loaded comment" in written
    assert "# keep loaded" in written
    assert "# disk changed later" not in written
    assert "# changed on disk" not in written
    assert "name: new-name" in written


def test_round_trip_write_preserves_unknown_blocks_and_section_extras(tmp_path):
    source = tmp_path / "source.yaml"
    source.write_text(
        "experiment:\n  name: old-name\ncloud:\n  custom_block:\n    keep: me\n",
        encoding="utf-8",
    )

    grouped = {
        "experiment": {"name": "new-name"},
        "cloud": {"custom_block": {"keep": "me"}},
    }

    write_grouped_config(
        grouped,
        output_path=tmp_path / "copy.yaml",
        source_roundtrip_path=source,
    )

    written = (tmp_path / "copy.yaml").read_text(encoding="utf-8")
    assert "name: new-name" in written
    assert "custom_block:" in written
    assert "keep: me" in written


def test_round_trip_write_preserves_empty_commented_loaded_section(tmp_path):
    source = tmp_path / "source.yaml"
    source.write_text(
        "experiment:\n  name: demo-exp\nworker:\n  # keep this empty section\n",
        encoding="utf-8",
    )

    write_grouped_config(
        {"experiment": {"name": "demo-exp-updated"}},
        output_path=tmp_path / "copy.yaml",
        source_roundtrip_path=source,
    )

    written = (tmp_path / "copy.yaml").read_text(encoding="utf-8")
    assert "name: demo-exp-updated" in written
    assert "worker:" in written
    assert "# keep this empty section" in written


def test_round_trip_write_preserves_empty_cloud_placeholders(tmp_path):
    source = tmp_path / "source.yaml"
    source.write_text(
        "cloud:\n"
        "  workers:\n"
        "    defaults:\n"
        "      instance_profile: gce-worker-n2d\n"
        "      count: 1\n"
        "    placements:\n"
        "      - {}\n",
        encoding="utf-8",
    )

    grouped = {
        "cloud": {
            "workers": {
                "defaults": {"instance_profile": "gce-worker-n2d", "count": 1},
                "placements": [{}],
            }
        }
    }

    write_grouped_config(
        grouped,
        output_path=tmp_path / "out.yaml",
        source_roundtrip_path=source,
    )

    assert "      - {}" in (tmp_path / "out.yaml").read_text(encoding="utf-8")


def test_round_trip_write_merge_failure_does_not_fall_back_to_normalized_yaml(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.yaml"
    output = tmp_path / "out.yaml"
    source.write_text("experiment:\n  name: old-name\n", encoding="utf-8")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("merge failed")

    monkeypatch.setattr(
        genconfig_core,
        "_merge_roundtrip_document",
        _boom,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="merge failed"):
        write_grouped_config(
            {"experiment": {"name": "new-name"}},
            output_path=output,
            source_roundtrip_path=source,
        )

    assert not output.exists()


def test_write_grouped_config_without_roundtrip_base_emits_normalized_yaml(tmp_path):
    output = tmp_path / "out.yaml"

    write_grouped_config(
        {"runtime": {"trials": 1}, "experiment": {"name": "demo-exp"}},
        output_path=output,
    )

    assert output.read_text(encoding="utf-8").strip() == (
        "runtime:\n  trials: 1\nexperiment:\n  name: demo-exp"
    )
