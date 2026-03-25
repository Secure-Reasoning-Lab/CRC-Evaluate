"""Add runtime cloud worker or evaluator capacity to an active launch."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from crsbench.cloud.bootstrap import CloudVmBootstrapInputs
from crsbench.cloud.cli._config_reconnect import (
    reconnect,
    resolve_effective_experiment_name,
)
from crsbench.cloud.cli._monitor import require_launch_state
from crsbench.cloud.expansion import (
    build_dynamic_placement_request,
    next_name_start_index,
    resolve_dynamic_placement_env,
)
from crsbench.cloud.launch_state import (
    CreatedCloudInstanceRecord,
    append_created_instance_records,
    append_fleet_records,
    save_launch_state,
)
from crsbench.cloud.providers import (
    provider_adapter_for_context,
    provisioner_for_context,
)
from crsbench.cloud.quota import CloudQuotaValidationError
from crsbench.cloud.status import CloudFleetStatusManager
from crsbench.distributed.registry import RuntimeRegistration
from crsbench.run_experiment import load_experiment_config
from crsbench.utils.logger import get_logger

if TYPE_CHECKING:
    import argparse


logger = get_logger(__name__)


def _count_role_fleet(fleets: list[object], role: str) -> int:
    return sum(
        int(getattr(fleet, "count", 0) or 0)
        for fleet in fleets
        if getattr(fleet, "role", role) == role
    )


def _log_addition_summary(context, request) -> None:
    worker_total = _count_role_fleet(context.worker_fleet_configs, "worker")
    evaluator_total = _count_role_fleet(context.evaluator_fleet_configs, "evaluator")
    delta_workers = request.count if request.role == "worker" else 0
    delta_evaluators = request.count if request.role == "evaluator" else 0
    logger.info("Add Cloud Placement")
    logger.info("Role: {}", request.role)
    logger.info("Source: runtime_added")
    logger.info("Instance profile: {}", request.instance_profile)
    logger.info("Delta count: +{}", request.count)
    if request.regions:
        logger.info("Effective regions: {}", ", ".join(request.regions))
    if request.zones:
        logger.info("Effective zones: {}", ", ".join(request.zones))
    logger.info("Fallback: {} (inherited)", str(request.fallback).lower())
    logger.info("Current totals:")
    logger.info(
        "orchestrators=1 workers={} evaluators={}",
        worker_total,
        evaluator_total,
    )
    logger.info("Projected totals after apply:")
    logger.info(
        "orchestrators=1 workers={} evaluators={}",
        worker_total + delta_workers,
        evaluator_total + delta_evaluators,
    )


def _apply_runtime_added_placement(
    args: argparse.Namespace, *, context, config, request
) -> int:
    launch_state = context.launch_state
    if launch_state is None:
        logger.error("Runtime-added cloud placement requires saved launch state")
        return 1

    experiment_name = launch_state.experiment_name
    config_path = Path(args.config)
    base_cwd = Path.cwd()
    fleet = None
    role = request.role
    provisioner = None
    create_attempted = False

    try:
        provisioner = provisioner_for_context(context)
        adapter = provider_adapter_for_context(context, provisioner=provisioner)

        if role == "worker":
            fleet = adapter.build_dynamic_worker_fleet(
                experiment_name=experiment_name,
                config=config,
                request=request,
                name_start_index=next_name_start_index(
                    context.worker_fleet_configs,
                    role="worker",
                ),
                cwd=base_cwd,
            )
            shortages = adapter.quota_shortages_for_dynamic_fleet(fleet=fleet)
            if shortages:
                raise CloudQuotaValidationError(shortages)
            registration = RuntimeRegistration.from_experiment_config(config)
            bootstrap_inputs = CloudVmBootstrapInputs.from_experiment_config(config)
            env_passthrough = resolve_dynamic_placement_env(
                config=config,
                role="worker",
                instance_profile=request.instance_profile,
                cwd=base_cwd,
            )
            _resolved_context, _redis_conn, readiness, _lifecycle, _filestore = (
                reconnect(
                    args.config,
                    experiment_name,
                    wait_for_remote_redis=True,
                )
            )
            manager = CloudFleetStatusManager(
                readiness_store=readiness,
                provisioner=provisioner,
            )
            create_attempted = True
            created_raw = provisioner.create_workers(
                experiment_name=experiment_name,
                fleet=fleet,
                redis_host=context.redis_host,
                redis_password=context.redis_password,
                registration=registration,
                bootstrap_inputs=bootstrap_inputs,
                env_passthrough=env_passthrough,
            )
            created_workers = [
                adapter.to_cloud_instance_record(
                    worker,
                    project=fleet.project,
                    ssh_via_iap=fleet.ssh_via_iap,
                    role="worker",
                )
                for worker in created_raw
            ]
            manager.wait_for_added_instances(
                experiment_name=experiment_name,
                workers=created_workers,
                evaluators=[],
                timeout_sec=fleet.readiness_timeout_sec,
            )
            updated_state = append_fleet_records(
                launch_state,
                workers=[
                    adapter.to_cloud_fleet_placement_record(
                        fleet,
                        role="worker",
                        placement_source="runtime_added",
                    )
                ],
            )
            save_launch_state(config_path, updated_state)
            try:
                append_created_instance_records(
                    config_path,
                    experiment_name=experiment_name,
                    records=[
                        CreatedCloudInstanceRecord(
                            provider=request.provider,
                            project=fleet.project,
                            zone=worker.zone,
                            instance_name=worker.name,
                        )
                        for worker in created_raw
                    ],
                )
            except OSError as exc:
                logger.warning(
                    "Saved launch state for runtime-added worker placement, but failed to append created-instance cache: {}",
                    exc,
                )
            return 0

        fleet = adapter.build_dynamic_evaluator_fleet(
            experiment_name=experiment_name,
            config=config,
            request=request,
            name_start_index=next_name_start_index(
                context.evaluator_fleet_configs,
                role="evaluator",
            ),
            cwd=base_cwd,
        )
        shortages = adapter.quota_shortages_for_dynamic_fleet(fleet=fleet)
        if shortages:
            raise CloudQuotaValidationError(shortages)
        registration = RuntimeRegistration.from_experiment_config(config)
        bootstrap_inputs = CloudVmBootstrapInputs.from_experiment_config(config)
        env_passthrough = resolve_dynamic_placement_env(
            config=config,
            role="evaluator",
            instance_profile=request.instance_profile,
            cwd=base_cwd,
        )
        _resolved_context, _redis_conn, readiness, _lifecycle, _filestore = reconnect(
            args.config,
            experiment_name,
            wait_for_remote_redis=True,
        )
        manager = CloudFleetStatusManager(
            readiness_store=readiness,
            provisioner=provisioner,
        )
        create_attempted = True
        created_raw = provisioner.create_evaluators(
            experiment_name=experiment_name,
            fleet=fleet,
            redis_host=context.redis_host,
            redis_password=context.redis_password,
            registration=registration,
            experiment_config_path=str(config_path),
            bootstrap_inputs=bootstrap_inputs,
            env_passthrough=env_passthrough,
        )
        created_evaluators = [
            adapter.to_cloud_instance_record(
                worker,
                project=fleet.project,
                ssh_via_iap=fleet.ssh_via_iap,
                role="evaluator",
            )
            for worker in created_raw
        ]
        manager.wait_for_added_instances(
            experiment_name=experiment_name,
            workers=[],
            evaluators=created_evaluators,
            timeout_sec=fleet.readiness_timeout_sec,
        )
        updated_state = append_fleet_records(
            launch_state,
            evaluators=[
                adapter.to_cloud_fleet_placement_record(
                    fleet,
                    role="evaluator",
                    placement_source="runtime_added",
                )
            ],
        )
        save_launch_state(config_path, updated_state)
        try:
            append_created_instance_records(
                config_path,
                experiment_name=experiment_name,
                records=[
                    CreatedCloudInstanceRecord(
                        provider=request.provider,
                        project=fleet.project,
                        zone=worker.zone,
                        instance_name=worker.name,
                    )
                    for worker in created_raw
                ],
            )
        except OSError as exc:
            logger.warning(
                "Saved launch state for runtime-added evaluator placement, but failed to append created-instance cache: {}",
                exc,
            )
        return 0
    except Exception as exc:
        if create_attempted and fleet is not None and provisioner is not None:
            try:
                if role == "worker":
                    provisioner.delete_workers(
                        experiment_name=experiment_name,
                        fleet=fleet,
                    )
                else:
                    provisioner.delete_evaluators(
                        experiment_name=experiment_name,
                        fleet=fleet,
                    )
            except Exception as rollback_exc:
                logger.warning(
                    "Best-effort rollback failed for runtime-added {} placement: {}",
                    role,
                    rollback_exc,
                )
        logger.error("Cloud {} failed: {}", args.cloud_command, exc)
        return 1


def run_add_capacity(args: argparse.Namespace) -> int:
    """Add one runtime worker or evaluator placement to an active launch."""
    role = "worker" if args.cloud_command == "add-workers" else "evaluator"
    try:
        experiment_name = resolve_effective_experiment_name(args.config, None)
        context = require_launch_state(args.config, experiment_name)
        config = load_experiment_config(args.config)
        request = build_dynamic_placement_request(
            role=role,
            config=config,
            instance_profile=args.instance_profile,
            count=args.count,
            regions=args.regions,
            zones=args.zones,
        )
        _log_addition_summary(context, request)
        if not args.force:
            if not sys.stdin.isatty():
                logger.error("Use --force for non-interactive runtime fleet expansion")
                return 1
            confirm = input("Type 'yes' to continue: ").strip().lower()
            if confirm != "yes":
                logger.info("Cancelled.")
                return 0
        return _apply_runtime_added_placement(
            args,
            context=context,
            config=config,
            request=request,
        )
    except SystemExit as exc:
        logger.error(str(exc))
        return 1
    except Exception as exc:
        logger.error("Cloud {} failed: {}", args.cloud_command, exc)
        return 1
