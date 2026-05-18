"""Tests for crsbench gen-config command."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from crsbench.genconfig.generator import (
    build_config_from_answers,
    discover_benchmark_suites,
    discover_crs_names,
    generate_config,
    render_config_yaml,
)


@pytest.fixture
def minimal_answers():
    """Minimal valid answers for config generation."""
    return {
        "name": "test-experiment",
        "task": "bugfinding",
        "mode": "delta",
        "benchmark_suite": "smoke/sanity",
        "crs_services": {"crs-libfuzzer": {"num_cores": 4}},
        "trials": 1,
        "max_total_time": 28800,
        "build_timeout": 3600,
        "run_timeout": 14400,
        "verify_timeout": 7200,
        "experiment_filestore": "./experiment-data",
        "report_filestore": "./report-data",
    }


class TestDiscovery:
    """Tests for auto-discovery of benchmark suites and CRS names."""

    def test_discover_benchmark_suites(self, tmp_path: Path):
        suites_dir = tmp_path / "suites"
        suites_dir.mkdir()
        (suites_dir / "alpha.yaml").write_text(
            "Name: alpha\nDescription: Alpha suite\nbenchmark_list:\n  - b1\n  - b2\n"
        )
        (suites_dir / "beta.yaml").write_text("Name: beta\n")
        (suites_dir / "not-yaml.txt").write_text("ignored\n")

        result = discover_benchmark_suites(suites_dir)
        names = [s["name"] for s in result]
        assert names == ["alpha", "beta"]
        assert result[0]["description"] == "Alpha suite"
        assert result[0]["count"] == 2

    def test_discover_benchmark_suites_missing_dir(self, tmp_path: Path):
        result = discover_benchmark_suites(tmp_path / "nonexistent")
        assert result == []

    def test_discover_crs_names(self, tmp_path: Path):
        registry = tmp_path / "registry"
        registry.mkdir()
        (registry / "crs-a.yaml").write_text("name: crs-a\n")
        (registry / "crs-b.yaml").write_text("name: crs-b\n")

        result = discover_crs_names(registry)
        assert result == ["crs-a", "crs-b"]

    def test_discover_crs_names_missing_dir(self, tmp_path: Path):
        result = discover_crs_names(tmp_path / "nonexistent")
        assert result == []

    def test_discover_real_benchmark_suites(self):
        """Verify discovery works with the actual repo suites directory."""
        suites = discover_benchmark_suites(Path("benchmark-suites"))
        if Path("benchmark-suites").is_dir():
            assert len(suites) > 0
            names = [s["name"] for s in suites]
            assert "smoke/sanity" in names

    def test_discover_real_crs_names(self):
        """Verify discovery works with the actual repo CRS registry."""
        names = discover_crs_names(Path("oss-crs/registry"))
        if Path("oss-crs/registry").is_dir():
            assert len(names) > 0
            assert "crs-libfuzzer" in names


class TestBuildConfig:
    """Tests for build_config_from_answers()."""

    def test_minimal_config(self, minimal_answers):
        config = build_config_from_answers(minimal_answers)

        assert config["experiment"]["name"] == "test-experiment"
        assert config["experiment"]["task"] == "bugfinding"
        assert config["experiment"]["mode"] == "delta"
        assert config["experiment"]["benchmark_suite"] == "smoke/sanity"
        assert "benchmarks" not in config["experiment"]
        assert config["crs_compose"] == {"crs-libfuzzer": {"num_cores": 4}}
        assert config["runtime"]["trials"] == 1
        assert config["runtime"]["max_total_time"] == 28800
        assert config["storage"]["experiment_filestore"] == "./experiment-data"
        assert config["storage"]["report_filestore"] == "./report-data"

    def test_with_benchmarks_instead_of_suite(self):
        answers = {
            "name": "bench-exp",
            "benchmarks": ["afc-curl-delta-01", "afc-libxml2-delta-02"],
            "crs_services": {"crs-codex": {"num_cores": 8}},
        }
        config = build_config_from_answers(answers)

        assert "benchmark_suite" not in config["experiment"]
        assert config["experiment"]["benchmarks"] == [
            "afc-curl-delta-01",
            "afc-libxml2-delta-02",
        ]

    def test_default_sanitizers_omitted(self, minimal_answers):
        """Default sanitizers (address) should not appear in output."""
        config = build_config_from_answers(minimal_answers)
        assert "sanitizers" not in config["experiment"]

    def test_custom_sanitizers_included(self, minimal_answers):
        minimal_answers["sanitizers"] = ["address", "undefined"]
        config = build_config_from_answers(minimal_answers)
        assert config["experiment"]["sanitizers"] == ["address", "undefined"]

    def test_defaults_applied(self):
        """Only name and crs_services are truly required."""
        config = build_config_from_answers(
            {"name": "defaults-test", "crs_services": {"crs-a": {"num_cores": 2}}}
        )
        assert config["experiment"]["task"] == "bugfinding"
        assert config["experiment"]["mode"] == "delta"
        assert config["runtime"]["trials"] == 1
        assert config["runtime"]["max_total_time"] == 28800
        assert config["storage"]["experiment_filestore"] == "./experiment-data"

    def test_distributed_fields(self):
        """Redis host, worker, and evaluator should appear when provided."""
        config = build_config_from_answers(
            {
                "name": "dist-exp",
                "crs_services": {"crs-a": {"num_cores": 4}},
                "redis_host": "redis.local:6379",
                "worker": {"jobs": 4, "cores_per_job": 8},
                "evaluator": {"jobs": 2, "cores_per_job": 4},
            }
        )
        assert config["runtime"]["redis_host"] == "redis.local:6379"
        assert config["worker"] == {"jobs": 4, "cores_per_job": 8}
        assert config["evaluator"] == {"jobs": 2, "cores_per_job": 4}

    def test_no_distributed_fields_by_default(self, minimal_answers):
        config = build_config_from_answers(minimal_answers)
        assert "redis_host" not in config["runtime"]
        assert "worker" not in config
        assert "evaluator" not in config

    def test_pov_early_stop(self):
        config = build_config_from_answers(
            {
                "name": "early-stop",
                "crs_services": {"crs-a": {"num_cores": 2}},
                "pov_early_stop": True,
            }
        )
        assert config["runtime"]["pov_early_stop"] is True

    def test_crs_mem_limit(self):
        config = build_config_from_answers(
            {
                "name": "mem-test",
                "crs_services": {"crs-a": {"num_cores": 4, "mem_limit": "16G"}},
            }
        )
        assert config["crs_compose"]["crs-a"]["mem_limit"] == "16G"

    def test_description(self):
        config = build_config_from_answers(
            {
                "name": "desc-test",
                "description": "Testing crs-codex on curl benchmarks",
                "crs_services": {"crs-a": {"num_cores": 2}},
            }
        )
        assert config["description"] == "Testing crs-codex on curl benchmarks"

    def test_no_description_by_default(self, minimal_answers):
        config = build_config_from_answers(minimal_answers)
        assert "description" not in config

    def test_litellm_external_with_budget(self):
        config = build_config_from_answers(
            {
                "name": "llm-test",
                "crs_services": {"crs-codex": {"num_cores": 4}},
                "litellm_mode": "external",
                "litellm_cost_budget": 10.0,
            }
        )
        assert config["runtime"]["litellm"]["mode"] == "external"
        assert config["runtime"]["litellm"]["cost_budget"] == 10.0

    def test_litellm_tracking_disabled(self):
        config = build_config_from_answers(
            {
                "name": "llm-notrack",
                "crs_services": {"crs-a": {"num_cores": 2}},
                "litellm_mode": "external",
                "llm_tracking_enabled": False,
            }
        )
        assert config["runtime"]["litellm"]["tracking_enabled"] is False

    def test_skip_litellm(self):
        config = build_config_from_answers(
            {
                "name": "fuzzer-only",
                "crs_services": {"crs-libfuzzer": {"num_cores": 4}},
                "skip_litellm": True,
            }
        )
        assert config["runtime"]["skip_litellm"] is True
        assert "litellm" not in config["runtime"]

    def test_no_litellm_by_default(self, minimal_answers):
        """Default litellm settings should not produce litellm/skip_litellm keys."""
        config = build_config_from_answers(minimal_answers)
        assert "skip_litellm" not in config["runtime"]
        assert "litellm" not in config["runtime"]


class TestRenderYaml:
    """Tests for YAML rendering."""

    def test_render_produces_valid_yaml(self, minimal_answers):
        config = build_config_from_answers(minimal_answers)
        yaml_str = render_config_yaml(config)

        # Should be parseable YAML
        parsed = yaml.safe_load(yaml_str)
        assert parsed is not None
        assert "experiment" in parsed
        assert "crs_compose" in parsed
        assert "runtime" in parsed
        assert "storage" in parsed

    def test_render_includes_comments(self, minimal_answers):
        config = build_config_from_answers(minimal_answers)
        yaml_str = render_config_yaml(config)

        assert "# CRSBench experiment configuration" in yaml_str
        assert "# Generated by: crsbench gen-config" in yaml_str
        assert "# Per-CRS resource allocation" in yaml_str
        assert "# resources:" in yaml_str

    def test_render_roundtrip_values(self, minimal_answers):
        config = build_config_from_answers(minimal_answers)
        yaml_str = render_config_yaml(config)
        parsed = yaml.safe_load(yaml_str)

        assert parsed["experiment"]["name"] == "test-experiment"
        assert parsed["crs_compose"]["crs-libfuzzer"]["num_cores"] == 4
        assert parsed["runtime"]["trials"] == 1
        assert parsed["storage"]["experiment_filestore"] == "./experiment-data"

    def test_render_distributed_sections(self):
        config = build_config_from_answers(
            {
                "name": "dist-render",
                "crs_services": {"crs-a": {"num_cores": 2}},
                "worker": {"jobs": 4, "cores_per_job": 8},
                "evaluator": {"jobs": 2, "cores_per_job": 4},
            }
        )
        yaml_str = render_config_yaml(config)
        parsed = yaml.safe_load(yaml_str)

        assert parsed["worker"]["jobs"] == 4
        assert parsed["evaluator"]["cores_per_job"] == 4
        assert "# Worker settings" in yaml_str
        assert "# Evaluator settings" in yaml_str


class TestGenerateConfig:
    """Tests for the generate_config() entry point."""

    def test_generate_writes_file(self, tmp_path: Path, minimal_answers):
        output = tmp_path / "config.yaml"
        result = generate_config(output_path=output, answers=minimal_answers)

        assert result is True
        assert output.exists()
        content = yaml.safe_load(output.read_text())
        assert content["experiment"]["name"] == "test-experiment"

    def test_generate_creates_parent_dirs(self, tmp_path: Path, minimal_answers):
        output = tmp_path / "sub" / "dir" / "config.yaml"
        result = generate_config(output_path=output, answers=minimal_answers)

        assert result is True
        assert output.exists()

    def test_generate_with_validation_pass(self, tmp_path: Path, minimal_answers):
        output = tmp_path / "validated.yaml"
        result = generate_config(
            output_path=output,
            answers=minimal_answers,
            validate=True,
        )

        assert result is True
        assert output.exists()

    def test_generate_with_validation_fail(self, tmp_path: Path):
        """Config missing required CRS services should fail validation."""
        bad_answers = {
            "name": "bad-experiment",
            "crs_services": {},  # Empty — will fail crs_compose validation
        }
        output = tmp_path / "bad.yaml"
        result = generate_config(
            output_path=output,
            answers=bad_answers,
            validate=True,
        )

        assert result is False
        assert not output.exists()
