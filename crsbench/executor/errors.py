"""Errors for the DAG executor."""


class CycleError(ValueError):
    """Raised when the dependency graph contains a cycle.

    Attributes:
        nodes: The nodes involved in the cycle.
    """

    def __init__(self, nodes: list[str]) -> None:
        self.nodes = nodes
        super().__init__(f"Dependency cycle detected involving: {nodes}")


class DependencyError(ValueError):
    """Raised when a job depends on an unknown job ID.

    Attributes:
        job_id: The job that has the invalid dependency.
        unknown_dep: The dependency ID that does not exist.
    """

    def __init__(self, job_id: str, unknown_dep: str) -> None:
        self.job_id = job_id
        self.unknown_dep = unknown_dep
        super().__init__(f"Job '{job_id}' depends on unknown job '{unknown_dep}'")
