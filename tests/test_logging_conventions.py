"""Regression checks for CRSBench logging conventions."""

from __future__ import annotations

import ast
import re
from pathlib import Path

_LOG_METHODS = {
    "trace",
    "debug",
    "info",
    "success",
    "warning",
    "error",
    "critical",
    "exception",
}
_PRINTF_PLACEHOLDER_RE = re.compile(
    r"%(\([^)]+\))?[#0\- +]?(?:\d+|\*)?(?:\.(?:\d+|\*))?[hlL]?[diouxXeEfFgGcrsa]"
)


def test_crsbench_uses_loguru_compatible_logging_patterns() -> None:
    """Reject stdlib logging conventions that do not work with CRSBench's Loguru setup."""
    violations: list[str] = []

    for path in sorted(Path("crsbench").rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "logging":
                        violations.append(
                            f"{path}:{node.lineno} uses stdlib logging import"
                        )
            elif isinstance(node, ast.ImportFrom):
                if node.module == "logging":
                    violations.append(
                        f"{path}:{node.lineno} uses stdlib logging import"
                    )
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr not in _LOG_METHODS:
                    continue

                if any(
                    keyword.arg == "exc_info"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                    for keyword in node.keywords
                ):
                    violations.append(f"{path}:{node.lineno} uses exc_info=True")

                if (
                    len(node.args) >= 2
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                    and _PRINTF_PLACEHOLDER_RE.search(node.args[0].value)
                ):
                    violations.append(
                        f"{path}:{node.lineno} uses printf-style placeholders"
                    )

    assert not violations, "Found Loguru-incompatible logging patterns:\n" + "\n".join(
        violations
    )
