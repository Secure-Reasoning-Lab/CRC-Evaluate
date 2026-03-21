"""Preparation helpers for the Atlantis/UniAFL coverage backend."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from crsbench.evaluation.adapter.compose_common import run_oss_crs_prepare
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_UNIAFL_IMAGE_PREFIX = "ghcr.io/team-atlanta"
DEFAULT_UNIAFL_REPO_URL = (
    "https://github.com/Team-Atlanta/atlantis-multilang-given_fuzzer.git"
)
DEFAULT_UNIAFL_ROOT_BASENAME = "atlantis-multilang-given_fuzzer"
DEFAULT_UNIAFL_RUNTIME_IMAGE_NAME = "multilang-given_fuzzer-crs"
DEFAULT_UNIAFL_RUNTIME_IMAGE_JVM_NAME = "multilang-given_fuzzer-crs"
DEFAULT_UNIAFL_BUILDER_IMAGE_NAME = "multilang-given_fuzzer-builder"
DEFAULT_UNIAFL_CLANG_IMAGE_NAME = "multilang-given_fuzzer-clang"
DEFAULT_UNIAFL_RELEASE = "1.0.0"
DEFAULT_UNIAFL_LOCAL_IMAGE_TAG = "latest"
UNIAFL_PREPARE_IMAGES = (
    "multilang-given_fuzzer-clang",
    "multilang-given_fuzzer-builder",
    "multilang-given_fuzzer-builder-jvm",
    "multilang-given_fuzzer-c-archive",
    "multilang-given_fuzzer-jvm-archive",
    "multilang-given_fuzzer-crs",
)
PREPARE_STATE_FILE = ".crsbench-uniafl-prepare.json"


def default_uniafl_root() -> Path:
    """Return the repository-local Atlantis checkout path."""
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "third_party" / DEFAULT_UNIAFL_ROOT_BASENAME


def _ensure_checkout(
    dest: Path,
    *,
    url: str = DEFAULT_UNIAFL_REPO_URL,
    ref: str = DEFAULT_UNIAFL_RELEASE,
) -> None:
    """Clone or update the Atlantis checkout to the pinned release.

    If *dest* does not exist, clones from *url* at *ref*.
    If it exists, fetches and resets to ``origin/{ref}``.
    """
    if dest.exists():
        logger.info(f"Updating Atlantis checkout at {dest} to {ref}")
        subprocess.run(
            ["git", "fetch", "--recurse-submodules", "origin", ref],
            cwd=dest,
            check=True,
            capture_output=True,
            text=True,
            errors="replace",
        )
        subprocess.run(
            ["git", "checkout", ref],
            cwd=dest,
            check=True,
            capture_output=True,
            text=True,
            errors="replace",
        )
    else:
        logger.info(f"Cloning Atlantis from {url} at {ref} to {dest}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "git",
                "clone",
                "--recurse-submodules",
                "--branch",
                ref,
                url,
                str(dest),
            ],
            check=True,
            capture_output=True,
            text=True,
            errors="replace",
        )


def default_uniafl_image_prefix() -> str:
    """Return the registry/repository prefix for Atlantis images."""
    return DEFAULT_UNIAFL_IMAGE_PREFIX


def qualify_uniafl_image(image_name: str, *, tag: str = DEFAULT_UNIAFL_RELEASE) -> str:
    """Return the fully qualified Atlantis image reference."""
    return f"{default_uniafl_image_prefix()}/{image_name}:{tag}"


def local_uniafl_image(
    image_name: str, *, tag: str = DEFAULT_UNIAFL_LOCAL_IMAGE_TAG
) -> str:
    """Return the canonical local Atlantis image reference."""
    return f"{image_name}:{tag}"


def prepare_image_refs(
    *, image_tag: str = DEFAULT_UNIAFL_LOCAL_IMAGE_TAG
) -> tuple[str, ...]:
    """Return the canonical local Atlantis prepare image references."""
    return tuple(
        local_uniafl_image(image_name, tag=image_tag)
        for image_name in UNIAFL_PREPARE_IMAGES
    )


def published_prepare_image_refs(
    *, image_tag: str = DEFAULT_UNIAFL_RELEASE
) -> tuple[str, ...]:
    """Return the published Atlantis prepare image references."""
    return tuple(
        qualify_uniafl_image(image_name, tag=image_tag)
        for image_name in UNIAFL_PREPARE_IMAGES
    )


def default_uniafl_runtime_image(language: str | None = None) -> str:
    """Return the default Atlantis runtime image reference for coverage runs."""
    if language and language.lower() == "jvm":
        return local_uniafl_image(DEFAULT_UNIAFL_RUNTIME_IMAGE_JVM_NAME)
    return local_uniafl_image(DEFAULT_UNIAFL_RUNTIME_IMAGE_NAME)


def default_uniafl_builder_image() -> str:
    """Return the default Atlantis builder image reference for coverage builds."""
    return local_uniafl_image(DEFAULT_UNIAFL_BUILDER_IMAGE_NAME)


def default_uniafl_clang_image() -> str:
    """Return the default Atlantis clang image reference for coverage tools."""
    return local_uniafl_image(DEFAULT_UNIAFL_CLANG_IMAGE_NAME)


def _local_image_exists(image_ref: str) -> bool:
    inspect = subprocess.run(
        ["docker", "image", "inspect", image_ref],
        capture_output=True,
        text=True,
        errors="replace",
    )
    return inspect.returncode == 0


def _local_image_id(image_ref: str) -> str | None:
    inspect = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image_ref],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if inspect.returncode != 0:
        return None
    image_id = inspect.stdout.strip()
    return image_id or None


def current_prepare_image_ids(
    *, image_tag: str = DEFAULT_UNIAFL_LOCAL_IMAGE_TAG
) -> dict[str, str]:
    """Return the local image IDs for the canonical Atlantis prepare images."""
    image_ids: dict[str, str] = {}
    for image_ref in prepare_image_refs(image_tag=image_tag):
        image_id = _local_image_id(image_ref)
        if image_id is not None:
            image_ids[image_ref] = image_id
    return image_ids


def _pull_prepare_images(*, image_tag: str = DEFAULT_UNIAFL_RELEASE) -> list[str]:
    failures: list[str] = []
    for image_name in UNIAFL_PREPARE_IMAGES:
        published_ref = qualify_uniafl_image(image_name, tag=image_tag)
        local_ref = local_uniafl_image(image_name)
        pull = subprocess.run(
            ["docker", "pull", published_ref],
            capture_output=True,
            text=True,
            errors="replace",
        )
        if pull.returncode != 0:
            detail = pull.stderr.strip() or pull.stdout.strip() or "unknown error"
            failures.append(f"{published_ref}: {detail}")
            continue
        tag_result = subprocess.run(
            ["docker", "tag", published_ref, local_ref],
            capture_output=True,
            text=True,
            errors="replace",
        )
        if tag_result.returncode != 0:
            detail = (
                tag_result.stderr.strip()
                or tag_result.stdout.strip()
                or "unknown error"
            )
            failures.append(f"{published_ref} -> {local_ref}: {detail}")
    return failures


def _checkout_matches_published_release(repo_root: Path) -> bool:
    tag = subprocess.run(
        ["git", "-C", str(repo_root), "describe", "--tags", "--exact-match"],
        capture_output=True,
        text=True,
        errors="replace",
    )
    tracked_status = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True,
        text=True,
        errors="replace",
    )
    return (
        tag.returncode == 0
        and tag.stdout.strip() == DEFAULT_UNIAFL_RELEASE
        and tracked_status.returncode == 0
        and tracked_status.stdout.strip() == ""
    )


def _current_prepare_fingerprint() -> str:
    hasher = hashlib.sha256()
    tracked_files = (
        Path(__file__).resolve(),
        Path(__file__).resolve().parents[1]
        / "evaluation"
        / "coverage"
        / "uniafl_runtime.py",
    )
    for path in tracked_files:
        hasher.update(path.name.encode())
        hasher.update(path.read_bytes())
    return hasher.hexdigest()


def _prepare_state_path(repo_root: Path) -> Path:
    return repo_root / PREPARE_STATE_FILE


def _uniafl_checkout_fingerprint(repo_root: Path) -> str:
    git_head = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        errors="replace",
    )
    git_status = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain"],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if git_head.returncode == 0 and git_status.returncode == 0:
        filtered_status = []
        for line in git_status.stdout.splitlines():
            path_text = line[3:] if len(line) > 3 else ""
            if path_text == PREPARE_STATE_FILE:
                continue
            filtered_status.append(line)
        return f"git:{git_head.stdout.strip()}:{'\n'.join(filtered_status)}"

    hasher = hashlib.sha256()
    fallback_files = [
        repo_root / "oss-crs" / "crs.yaml",
        repo_root / "oss-crs" / "docker-bake.hcl",
        repo_root / "bin" / "compile_target",
    ]
    for path in fallback_files:
        if path.exists():
            hasher.update(path.name.encode())
            hasher.update(path.read_bytes())
    return hasher.hexdigest()


def current_uniafl_checkout_fingerprint(uniafl_root: Path | None = None) -> str:
    """Return the fingerprint for the selected Atlantis checkout."""
    repo_root = Path(uniafl_root or default_uniafl_root()).resolve()
    return _uniafl_checkout_fingerprint(repo_root)


def _prepare_state_matches(repo_root: Path) -> bool:
    state_path = _prepare_state_path(repo_root)
    if not state_path.exists():
        return False
    try:
        state = json.loads(state_path.read_text())
    except json.JSONDecodeError:
        return False
    return (
        state.get("fingerprint") == _current_prepare_fingerprint()
        and state.get("checkout_fingerprint") == _uniafl_checkout_fingerprint(repo_root)
        and state.get("image_ids") == current_prepare_image_ids()
    )


def _write_prepare_state(repo_root: Path) -> None:
    _prepare_state_path(repo_root).write_text(
        json.dumps(
            {
                "fingerprint": _current_prepare_fingerprint(),
                "checkout_fingerprint": _uniafl_checkout_fingerprint(repo_root),
                "image_ids": current_prepare_image_ids(),
            },
            indent=2,
        )
    )


def write_prepare_state(repo_root: Path) -> None:
    """Record that the local Atlantis prepare image contract matches this checkout."""
    _write_prepare_state(repo_root)


def get_uniafl_prepare_readiness(
    uniafl_root: Path | None = None,
    *,
    image_tag: str = DEFAULT_UNIAFL_LOCAL_IMAGE_TAG,
) -> tuple[Path, list[str]]:
    """Return the selected Team Atlanta Atlantis checkout and readiness issues."""
    repo_root = Path(uniafl_root or default_uniafl_root()).resolve()
    issues: list[str] = []

    state_path = _prepare_state_path(repo_root)
    if not _prepare_state_matches(repo_root):
        if state_path.exists():
            issues.append(f"stale prepare state: {state_path}")
        else:
            issues.append(f"missing prepare state: {state_path}")

    missing_images = [
        image_ref
        for image_ref in prepare_image_refs(image_tag=image_tag)
        if not _local_image_exists(image_ref)
    ]
    issues.extend(f"missing local image: {image_ref}" for image_ref in missing_images)
    return repo_root, issues


def prepare_uniafl_backend(
    uniafl_root: Path | None = None,
    *,
    oss_crs_cmd: str = "oss-crs",
    timeout: int = 3600,
) -> int:
    """Prepare the Atlantis coverage backend via oss-crs prepare."""
    repo_root = Path(uniafl_root or default_uniafl_root()).resolve()
    _ensure_checkout(repo_root)
    if not (repo_root / "oss-crs" / "crs.yaml").exists():
        raise FileNotFoundError(
            f"Atlantis checkout not found or incomplete after clone/pull: {repo_root}"
        )

    logger.info(f"Preparing UniAFL coverage backend from {repo_root}")
    _, readiness_issues = get_uniafl_prepare_readiness(repo_root)
    if not readiness_issues:
        return 0

    published_release_checkout = _checkout_matches_published_release(repo_root)
    missing_image_issues = [
        issue for issue in readiness_issues if issue.startswith("missing local image:")
    ]
    if missing_image_issues and published_release_checkout:
        logger.info("Pulling canonical Atlantis prepare images from GHCR")
        pull_failures = _pull_prepare_images()
        if not pull_failures:
            _, readiness_issues = get_uniafl_prepare_readiness(repo_root)
            if not any(
                issue.startswith("missing local image:") for issue in readiness_issues
            ):
                _write_prepare_state(repo_root)
                return 0
        else:
            logger.warning(
                "Falling back to local Atlantis image build after GHCR pull failed: %s",
                "; ".join(pull_failures),
            )
    if published_release_checkout and not any(
        issue.startswith("missing local image:") for issue in readiness_issues
    ):
        _write_prepare_state(repo_root)
        return 0

    control_root = repo_root / ".crsbench-oss-crs-prepare"
    compose_file = control_root / "crs-compose.yaml"
    work_dir = control_root / "oss-crs-workdir"

    from crsbench.evaluation.coverage.uniafl_runtime import write_coverage_compose_yaml

    write_coverage_compose_yaml(
        compose_path=compose_file,
        uniafl_root=repo_root,
    )

    stdout, stderr, returncode = run_oss_crs_prepare(
        compose_file,
        work_dir,
        oss_crs_cmd=oss_crs_cmd,
        timeout=timeout,
    )
    if returncode != 0:
        detail = stderr or stdout
        raise RuntimeError(f"oss-crs prepare failed (rc={returncode}): {detail}")

    _write_prepare_state(repo_root)
    return 0
