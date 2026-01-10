"""Experiment validation for the reporting module."""

from pathlib import Path

from crsbench.reporting.models import ExperimentValidationResult
from crsbench.reporting.snapshot_loader import discover_trials
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


class ExperimentValidator:
    """Validate experiment completeness and detect issues.

    This class checks for:
    - Missing trial metadata
    - Invalid trial metadata
    - Incomplete snapshots

    Example:
        validator = ExperimentValidator()
        result = validator.validate_experiment_completeness(experiment_dir)
        if not result.is_complete():
            print(validator.generate_completeness_report(result))
    """

    def validate_experiment_completeness(
        self, experiment_dir: Path
    ) -> ExperimentValidationResult:
        """Validate experiment completeness and detect missing trials.

        Args:
            experiment_dir: Path to experiment directory

        Returns:
            Validation result with missing/invalid trials
        """
        trials = discover_trials(experiment_dir)

        valid_trials = [t for t in trials if t.status == "valid"]
        missing_metadata_trials = [t for t in trials if t.status == "missing_metadata"]
        invalid_metadata_trials = [t for t in trials if t.status == "invalid_metadata"]

        # Combine invalid trials for the result
        invalid_trials = missing_metadata_trials + invalid_metadata_trials

        result = ExperimentValidationResult(
            experiment_dir=str(experiment_dir),
            total_discovered=len(trials),
            valid_count=len(valid_trials),
            missing_metadata_count=len(missing_metadata_trials),
            invalid_metadata_count=len(invalid_metadata_trials),
            valid_trials=valid_trials,
            invalid_trials=invalid_trials,
        )

        logger.info(
            f"Experiment validation complete: "
            f"{result.valid_count}/{result.total_discovered} valid, "
            f"{result.missing_metadata_count} missing metadata, "
            f"{result.invalid_metadata_count} invalid metadata"
        )

        return result

    def generate_completeness_report(
        self, validation_result: ExperimentValidationResult
    ) -> str:
        """Generate human-readable completeness report.

        Args:
            validation_result: Result from validate_experiment_completeness

        Returns:
            Formatted report string
        """
        lines = []
        lines.append("=" * 60)
        lines.append("Experiment Completeness Report")
        lines.append("=" * 60)
        lines.append(f"Experiment: {validation_result.experiment_dir}")
        lines.append(f"Total discovered trials: {validation_result.total_discovered}")
        lines.append(f"Valid trials:            {validation_result.valid_count}")
        lines.append(
            f"Missing metadata:        {validation_result.missing_metadata_count}"
        )
        lines.append(
            f"Invalid metadata:        {validation_result.invalid_metadata_count}"
        )

        if validation_result.is_complete():
            lines.append("")
            lines.append("Status: COMPLETE - All trials are valid")
        else:
            lines.append("")
            lines.append("Status: INCOMPLETE - Issues detected")

        if validation_result.invalid_trials:
            lines.append("")
            lines.append("Invalid Trials:")
            lines.append("-" * 40)
            for trial in validation_result.invalid_trials:
                lines.append(
                    f"  - Trial {trial.trial_num} ({trial.crs} on {trial.benchmark})"
                )
                lines.append(f"    Path: {trial.trial_dir}")
                lines.append(f"    Status: {trial.status}")
                if trial.error:
                    lines.append(f"    Error: {trial.error}")

        if validation_result.valid_trials:
            lines.append("")
            lines.append(f"Valid Trials ({len(validation_result.valid_trials)}):")
            lines.append("-" * 40)
            for trial in validation_result.valid_trials:
                lines.append(
                    f"  - Trial {trial.trial_num} ({trial.crs} on {trial.benchmark})"
                )
                lines.append(
                    f"    Snapshots: {trial.complete_snapshot_count}/{trial.snapshot_count}"
                )

        lines.append("")
        lines.append("=" * 60)

        return "\n".join(lines)
