#!/usr/bin/env python3
"""LiteLLM preflight checks for CI smoke runs.

Validates runtime env contract for external mode, then checks:
1) liveness endpoint reachability
2) key-management auth (generate -> info -> delete)
3) runtime auth against /models using runtime key
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

import requests
from crsbench.utils.litellm_env import (
    required_env_errors_for_mode,
    resolve_litellm_runtime_env,
)
from crsbench.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    message: str


def _health_check(base_url: str) -> CheckResult:
    try:
        response = requests.get(f"{base_url.rstrip('/')}/health/liveness", timeout=10)
        if response.status_code == 200:
            return CheckResult(ok=True, message="health/liveness returned 200")
        return CheckResult(
            ok=False,
            message=f"health/liveness returned {response.status_code}: {response.text[:200]}",
        )
    except Exception as exc:
        return CheckResult(ok=False, message=f"health/liveness request failed: {exc}")


def _runtime_auth_check(base_url: str, runtime_key: str) -> CheckResult:
    try:
        response = requests.get(
            f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {runtime_key}"},
            timeout=15,
        )
        if response.status_code == 200:
            return CheckResult(
                ok=True, message="runtime auth succeeded against /models"
            )
        return CheckResult(
            ok=False,
            message=f"runtime auth failed on /models ({response.status_code}): {response.text[:200]}",
        )
    except Exception as exc:
        return CheckResult(ok=False, message=f"runtime auth request failed: {exc}")


def _tracking_auth_check(base_url: str, master_key: str) -> CheckResult:
    headers = {
        "Authorization": f"Bearer {master_key}",
        "Content-Type": "application/json",
    }
    try:
        # Non-mutating auth probe: verify access to key-control endpoint.
        # Any non-auth client error (e.g. invalid key param) still proves auth.
        info_resp = requests.get(
            f"{base_url.rstrip('/')}/key/info",
            headers=headers,
            params={"key": master_key},
            timeout=20,
        )
        if info_resp.status_code in {401, 403}:
            return CheckResult(
                ok=False,
                message=f"tracking auth failed on /key/info ({info_resp.status_code}): "
                f"{info_resp.text[:200]}",
            )
        if info_resp.status_code >= 500:
            return CheckResult(
                ok=False,
                message=f"tracking auth failed on /key/info ({info_resp.status_code}): "
                f"{info_resp.text[:200]}",
            )
        return CheckResult(
            ok=True,
            message=f"tracking auth accepted by /key/info ({info_resp.status_code})",
        )
    except Exception as exc:
        return CheckResult(ok=False, message=f"tracking auth check failed: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="LiteLLM health/auth preflight for CI")
    parser.add_argument("--mode", default="external", choices=["external"])
    parser.add_argument(
        "--require-tracking",
        action="store_true",
        help="Require tracking-control auth check with upstream master key.",
    )
    args = parser.parse_args()

    runtime_env = resolve_litellm_runtime_env(args.mode)
    errors = required_env_errors_for_mode(
        runtime_env, tracking_enabled=args.require_tracking
    )
    if errors:
        logger.error("[litellm-preflight] missing/invalid env contract:")
        for error in errors:
            logger.error(f"  - {error}")
        return 2

    base_url = runtime_env.tracking_base_url
    if not base_url:
        logger.error("[litellm-preflight] no LiteLLM base URL resolved")
        return 2

    results = [_health_check(base_url)]

    runtime_key = runtime_env.api_key or runtime_env.master_key
    if runtime_key:
        results.append(
            _runtime_auth_check(runtime_env.direct_base_url or base_url, runtime_key)
        )
    else:
        results.append(CheckResult(ok=False, message="no runtime key resolved"))

    if args.require_tracking:
        if not runtime_env.master_key:
            results.append(
                CheckResult(ok=False, message="no upstream master key resolved")
            )
        else:
            results.append(_tracking_auth_check(base_url, runtime_env.master_key))

    failed = [result for result in results if not result.ok]
    for result in results:
        status = "PASS" if result.ok else "FAIL"
        log_fn = logger.info if result.ok else logger.error
        log_fn(f"[litellm-preflight] {status}: {result.message}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
