#!/usr/bin/env python3
"""Mock helper.py for testing exit code handling."""

import sys
import time


def main():
    args = sys.argv[1:]

    # Find testcase: last arg before "--" or last arg if no "--"
    if "--" in args:
        idx = args.index("--")
        testcase_path = args[idx - 1]
    else:
        testcase_path = args[-1]

    with open(testcase_path, "rb") as f:
        content = f.read()

    if b"HANG" in content:
        time.sleep(3600)
    elif b"TIMEOUT" in content:
        sys.exit(124)
    elif b"ASAN" in content:
        sys.exit(77)
    elif b"LEAK" in content:
        print("==18==ERROR: LeakSanitizer: detected memory leaks")
        print("Direct leak of 5600 byte(s) in 100 object(s)")
        print("SUMMARY: AddressSanitizer: 5600 byte(s) leaked in 100 allocation(s).")
        sys.exit(1)
    elif b"CRASH" in content:
        print("==ERROR: AddressSanitizer: SEGV")
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
