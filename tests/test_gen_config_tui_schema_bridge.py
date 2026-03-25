from crsbench.genconfig_tui.core import build_grouped_config
from crsbench.genconfig_tui.schema_bridge import validate_grouped_config


def test_validate_grouped_config_round_trips_against_crsbench():
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
            "resources": {
                "cores_per_trial": 2,
                "memory_per_trial": "4G",
            },
            "worker": {},
            "evaluator": {},
            "crs_compose": {
                "service_name": "crs-libfuzzer",
                "service_num_cores": 2,
                "infra_shared": True,
            },
            "cloud": {},
        }
    )

    model = validate_grouped_config(grouped)

    assert model.experiment == "demo-exp"
    assert model.task == "bugfixing"
    assert model.inputs.pov.enabled is True
    assert model.crs_compose is not None
    assert model.crs_compose.services["crs-libfuzzer"].num_cores == 2
