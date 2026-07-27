#!/usr/bin/env python3
"""Run a command with a temporary Foam-Agent-compatible Codex OAuth cache."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit("usage: run_with_codex_auth.py COMMAND [ARG ...]")

    source = Path("/mnt/c/Users/asus/.codex/auth.json")
    data = json.loads(source.read_text(encoding="utf-8"))
    tokens = data.get("tokens", {})
    access = tokens.get("access_token")
    account_id = tokens.get("account_id")
    if not isinstance(access, str) or not access:
        raise SystemExit("Codex auth cache has no tokens.access_token")

    with tempfile.TemporaryDirectory(prefix="foamagent-auth-", dir="/run") as home:
        profile_path = (
            Path(home)
            / ".clawdbot"
            / "agents"
            / "main"
            / "agent"
            / "auth-profiles.json"
        )
        profile_path.parent.mkdir(parents=True, mode=0o700)
        profile = {
            "profiles": {
                "openai-codex:default": {
                    "access": access,
                    "accountId": account_id,
                }
            }
        }
        profile_path.write_text(json.dumps(profile), encoding="utf-8")
        profile_path.chmod(0o600)

        env = os.environ.copy()
        env.pop("CODEX_HOME", None)
        env["HOME"] = home
        env.setdefault("HF_HOME", "/root/.cache/huggingface")
        return subprocess.run(sys.argv[1:], env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
