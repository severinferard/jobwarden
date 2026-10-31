#!/usr/bin/env python3
"""Claude Code PostToolUse hook (Bash): when a command starts a job, remind the session to watch it.

Silent otherwise, and never fails the tool call.
"""

import json
import re
import sys

LAUNCH = re.compile(r"\bjobwarden\s+run\b")
REMINDER = (
    "This command started a job. If it will run longer than a few minutes, start `jobwarden wait <name>` as a background "
    "command now, so this session is woken when it ends or goes wrong, and tell the user about `jobwarden watch <name>`."
)


def main():
    command = (json.load(sys.stdin).get("tool_input") or {}).get("command") or ""
    if LAUNCH.search(command):
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": REMINDER}}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
