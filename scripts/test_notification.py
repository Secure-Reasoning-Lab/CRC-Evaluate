#!/usr/bin/env python3
"""Send or preview a CRSBench Apprise notification."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from crsbench.utils.apprise_notify import (
    AppriseNotificationConfig,
    load_apprise_notification_config,
    send_apprise_message,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BODY = "CRSBench notification test"


def _load_project_env() -> None:
    load_dotenv(PROJECT_ROOT / ".env", override=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send a CRSBench test notification")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the resolved notification config and message without sending",
    )
    parser.add_argument(
        "--body",
        default=DEFAULT_BODY,
        help=f"Notification body to send (default: {DEFAULT_BODY!r})",
    )
    parser.add_argument(
        "--title-suffix",
        default="test",
        help="Optional title suffix for the test notification (default: test)",
    )
    parser.add_argument(
        "--no-dotenv",
        action="store_true",
        help="Do not load .env from the repository root before resolving config",
    )
    return parser


def _print_config(config: AppriseNotificationConfig, *, body: str) -> None:
    print(f"Resolved notification config: targets={len(config.urls)}")
    print(f"Title: {config.title}")
    print(f"Tag: {config.tag or '(none)'}")
    print(f"Body: {body}")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if not args.no_dotenv:
        _load_project_env()

    config = load_apprise_notification_config()
    if config is None:
        print(
            "Notification config missing. Set CRSBENCH_NOTIFY_APPRISE_URLS in your environment or .env.",
            file=sys.stdout,
        )
        return 1

    _print_config(config, body=args.body)

    if args.dry_run:
        print("Dry run only. Notification not sent.")
        return 0

    print("Sending test notification...")
    if send_apprise_message(config, body=args.body, title_suffix=args.title_suffix):
        print("Notification delivered successfully.")
        return 0

    print("Notification delivery failed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
