#!/usr/bin/env python3
"""Claude Code SessionStart hook: a plugin can't install the jobwarden command, so say so if it's missing."""

import json
import shutil

if not shutil.which("jobwarden"):
    message = "The jobwarden plugin needs the jobwarden command. Install it with: uv tool install jobwarden (or pipx install jobwarden)"
    print(json.dumps({"systemMessage": message}))
