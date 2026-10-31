#!/usr/bin/env python3
"""Claude Code PostToolUse hook (Bash): when a command starts a job, remind the session to watch it.

Silent otherwise, and never fails the tool call. In ~/.claude/settings.json, under hooks.PostToolUse with matcher "Bash".
"""

import json
import re
import sys

LAUNCH = re.compile(r"\bjobwarden\s+run\b")
REMINDER = (
    "This command started a job. If it will run longer than about 5 minutes, start `jobwarden wait <name>` as a background "
    "command now, so this session is woken if the job fails or stalls, and tell the user `jobwarden watch <name>`."
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
