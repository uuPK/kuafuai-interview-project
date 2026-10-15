"""Reproducible offline regression; real Docker opt-in, paid services always excluded."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def environment(source, *, docker=False):
    result = dict(source)
    for key in list(result):
        if key.startswith("NEWS_") and key.endswith(("_LIVE", "_REPLAY")):
            result.pop(key)
    result.update(
        NEWS_DOCKER_LIVE="1" if docker else "0",
        NEWS_COMPOSE_LIVE="1" if docker else "0",
        NEWS_REVIEW_LIVE="0",
        NEWS_RESEARCH_LIVE="0",
        NEWS_SERVICES_LIVE="0",
        NEWS_PHASE_THREE_RECORD="1",
        NEWS_DEMO_RECORD="1",
    )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="离线核心回归、覆盖率、前端及可选真实Docker验收")
    parser.add_argument("--docker", action="store_true")
    parser.add_argument("--node", default=shutil.which("node"))
    args = parser.parse_args(argv)
    if not args.node:
        parser.error("需要 Node.js；用 --node 指定可执行文件")
    root = Path(__file__).resolve().parent.parent
    report = root / "data/acceptance/final-regression"
    report.mkdir(parents=True, exist_ok=True)
    env = environment(os.environ, docker=args.docker)
    with tempfile.TemporaryDirectory(prefix="news-verify-docker-") as temporary:
        if args.docker:
            config = Path(temporary) / "config.json"
            config.write_text(json.dumps({"auths": {}, "proxies": {}}), encoding="utf-8")
            env["DOCKER_CONFIG"] = temporary
            if os.name == "nt":
                env.setdefault("DOCKER_HOST", "npipe:////./pipe/dockerDesktopLinuxEngine")
        commands = [
            [sys.executable, "-m", "ruff", "check", "app", "tests", "scripts"],
            [sys.executable, "-m", "ruff", "format", "--check", "app", "tests", "scripts"],
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--cov=app",
                "--cov-report=term:skip-covered",
                f"--cov-report=json:{report / 'coverage.json'}",
                f"--junitxml={report / 'tests.xml'}",
            ],
            [args.node, "--test", "tests/test_frontend.mjs", "tests/test_frontend_state.mjs"],
        ]
        for command in commands:
            result = subprocess.run(command, cwd=root, env=env, check=False)
            if result.returncode:
                return result.returncode
    print("Verification passed. Reports: data/acceptance/final-regression")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
