"""Replay policy helpers for incremental (snapshot/replay) builds."""

from __future__ import annotations

import os
from dataclasses import dataclass

REPLAY_MODE_OFF = "off"
REPLAY_MODE_ENFORCE = "enforce"
REPLAY_SOURCE_CRSBENCH_SHIM = "crsbench-shim"


@dataclass(frozen=True)
class ReplayPolicyConfig:
    """Resolved replay policy settings for one operation."""

    enabled: bool
    mode: str
    tool_hooks: tuple[str, ...]
    source: str

    def tool_hooks_csv(self) -> str:
        return ",".join(self.tool_hooks)


def resolve_replay_policy(*, inc_build: bool) -> ReplayPolicyConfig:
    """Resolve replay policy from execution mode.

    In inc-build mode, enforcement is mandatory.
    """
    if not inc_build:
        return ReplayPolicyConfig(
            enabled=False,
            mode=REPLAY_MODE_OFF,
            tool_hooks=(),
            source=REPLAY_SOURCE_CRSBENCH_SHIM,
        )

    hooks_env = os.getenv("CRSBENCH_REPLAY_POLICY_TOOLS", "maven,gradle")
    tool_hooks = tuple(p.strip().lower() for p in hooks_env.split(",") if p.strip())
    if not tool_hooks:
        tool_hooks = ("maven", "gradle")

    return ReplayPolicyConfig(
        enabled=True,
        mode=REPLAY_MODE_ENFORCE,
        tool_hooks=tool_hooks,
        source=REPLAY_SOURCE_CRSBENCH_SHIM,
    )


def replay_install_script(policy: ReplayPolicyConfig) -> str:
    """Build shell script that installs Chronos-style binary hooks in-image."""
    if not policy.enabled:
        return "exit 0"

    tools: list[str] = []
    if "maven" in policy.tool_hooks:
        tools.extend(["mvn", "mvnw"])
    if "gradle" in policy.tool_hooks:
        tools.extend(["gradle", "gradlew"])

    lines = [
        "set -eu",
        "install_hook() {",
        '  tool="$1"',
        '  bin="$2"',
        '  [ -n "$bin" ] || return 0',
        '  [ -x "$bin" ] || return 0',
        '  real="$bin.crsbench.real"',
        '  if [ -f "$real" ]; then return 0; fi',
        '  mv "$bin" "$real"',
        "  cat > \"$bin\" <<'CRSBENCH_SHIM'",
        _shim_template(),
        "CRSBENCH_SHIM",
        '  chmod +x "$bin"',
        "}",
        "# Hook PATH-discovered tools",
        "for tool in mvn mvnw gradle gradlew; do",
        f'  case " {" ".join(tools)} " in *" $tool "*) ;; *) continue ;; esac',
        '  bin="$(command -v "$tool" || true)"',
        '  install_hook "$tool" "$bin"',
        "done",
        "# Hook explicit tool paths configured in benchmark env",
        'if [ -n "${MVN:-}" ]; then install_hook mvn "$MVN"; fi',
        'if [ -n "${GRADLE:-}" ]; then install_hook gradle "$GRADLE"; fi',
    ]
    return "\n".join(lines)


def _shim_template() -> str:
    return """#!/bin/bash
set -eu

tool=\"$(basename \"$0\")\"
real=\"$0.crsbench.real\"
if [ ! -x \"$real\" ]; then
  echo \"[crsbench-replay] missing real binary: $real\" >&2
  exit 127
fi

strip_clean_tokens() {
  local kept=()
  local removed=0
  local arg
  for arg in \"$@\"; do
    case \"$tool:$arg\" in
      mvn:clean|mvnw:clean|mvn:clean:clean|mvnw:clean:clean)
        removed=1
        continue
        ;;
      gradle:clean|gradlew:clean|gradle::clean|gradlew::clean|gradle:*:clean|gradlew:*:clean)
        removed=1
        continue
        ;;
    esac
    kept+=(\"$arg\")
  done

  if [ \"$removed\" -eq 1 ]; then
    echo \"[crsbench-replay] stripped clean from $tool command\" >&2
  fi

  if [ \"${#kept[@]}\" -eq 0 ]; then
    echo \"[crsbench-replay] $tool clean-only command converted to no-op\" >&2
    exit 0
  fi
  REAL_ARGS=(\"${kept[@]}\")
}

strip_clean_tokens \"$@\"
exec \"$real\" \"${REAL_ARGS[@]}\"
"""
