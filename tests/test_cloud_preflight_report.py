from __future__ import annotations

import json


def test_derive_cloud_preflight_verdict_ready_when_all_checks_pass():
    from crsbench.cloud.preflight_report import (
        CloudPreflightCheck,
        CloudPreflightCheckStatus,
        CloudPreflightVerdict,
        derive_cloud_preflight_verdict,
    )

    checks = [
        CloudPreflightCheck(
            name="duplicate_launch_guard",
            status=CloudPreflightCheckStatus.PASS,
            summary="No saved launch state or live fleet conflict detected.",
        ),
        CloudPreflightCheck(
            name="quota",
            status="pass",
            summary="Quota is sufficient for the requested launch plan.",
        ),
    ]

    assert derive_cloud_preflight_verdict(checks) is CloudPreflightVerdict.READY


def test_derive_cloud_preflight_verdict_warning_when_any_check_warns():
    from crsbench.cloud.preflight_report import (
        CloudPreflightCheck,
        CloudPreflightCheckStatus,
        CloudPreflightVerdict,
        derive_cloud_preflight_verdict,
    )

    checks = [
        CloudPreflightCheck(
            name="remote_experiment_root_fallback",
            status=CloudPreflightCheckStatus.WARNING,
            summary="Standalone collect and teardown will fall back to the legacy remote path.",
        ),
        CloudPreflightCheck(
            name="duplicate_launch_guard",
            status="pass",
            summary="No saved launch state or live fleet conflict detected.",
        ),
    ]

    assert derive_cloud_preflight_verdict(checks) is CloudPreflightVerdict.WARNING


def test_derive_cloud_preflight_verdict_blocked_when_any_check_fails():
    from crsbench.cloud.preflight_report import (
        CloudPreflightCheck,
        CloudPreflightCheckStatus,
        CloudPreflightVerdict,
        derive_cloud_preflight_verdict,
    )

    checks = [
        CloudPreflightCheck(
            name="duplicate_launch_guard",
            status=CloudPreflightCheckStatus.FAIL,
            summary="Saved launch state already exists.",
        ),
        CloudPreflightCheck(
            name="quota",
            status="warning",
            summary="Quota is close but still sufficient.",
        ),
    ]

    assert derive_cloud_preflight_verdict(checks) is CloudPreflightVerdict.BLOCKED


def test_cloud_preflight_report_as_dict_uses_canonical_shape():
    from crsbench.cloud.preflight_report import (
        CloudPreflightCheck,
        CloudPreflightCheckStatus,
        build_cloud_preflight_report,
    )

    report = build_cloud_preflight_report(
        experiment="test-exp",
        provider="gce",
        plan={
            "orchestrator": {"instance_profile": "gce-orchestrator-n2d"},
            "workers": [],
            "evaluators": [],
        },
        resolved_defaults={"readiness_timeout_sec": 120},
        env_summary={
            "orchestrator": {
                "layer_order": ["cloud.env", "runtime_managed"],
                "layers": [{"name": "cloud.env", "key_count": 1, "keys": ["HF_TOKEN"]}],
            },
            "workers": [],
            "evaluators": [],
        },
        checks=[
            CloudPreflightCheck(
                name="duplicate_launch_guard",
                status=CloudPreflightCheckStatus.PASS,
                summary="No saved launch state or live fleet conflict detected.",
            ),
            CloudPreflightCheck(
                name="remote_experiment_root_fallback",
                status="warning",
                summary="Standalone collect and teardown will fall back to the legacy remote path.",
            ),
        ],
        reconnect_notes=[
            "status, events, and monitor require control-plane reachability.",
        ],
    )

    payload = report.as_dict()

    assert payload["schema_version"] == 1
    assert payload["experiment"] == "test-exp"
    assert payload["provider"] == "gce"
    assert payload["verdict"] == "warning"
    assert set(payload) == {
        "schema_version",
        "experiment",
        "provider",
        "verdict",
        "plan",
        "resolved_defaults",
        "env_summary",
        "checks",
        "reconnect_notes",
    }
    assert "warnings" not in payload
    assert "errors" not in payload
    assert payload["checks"] == [
        {
            "name": "duplicate_launch_guard",
            "status": "pass",
            "summary": "No saved launch state or live fleet conflict detected.",
            "detail": None,
        },
        {
            "name": "remote_experiment_root_fallback",
            "status": "warning",
            "summary": "Standalone collect and teardown will fall back to the legacy remote path.",
            "detail": None,
        },
    ]
    assert json.loads(json.dumps(payload)) == payload
