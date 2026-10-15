"""Build public application/tool images from an isolated ASCII-path allowlisted context."""

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def prepare_context(project, target):
    for filename in (".dockerignore", "pyproject.toml", "uv.lock"):
        shutil.copyfile(project / filename, target / filename)
    for folder in ("app", "docker"):
        shutil.copytree(
            project / folder,
            target / folder,
            ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", "*.pyo", ".env*", "*.key", "*.pem"
            ),
        )


def main():
    parser = argparse.ArgumentParser(description="在临时 ASCII 路径构建应用和工具镜像")
    parser.add_argument("--python-image", default="python:3.13-slim-bookworm")
    parser.add_argument("--docker-cli-image", default="docker:29-cli")
    parser.add_argument("--uv-image", default="ghcr.io/astral-sh/uv:0.11.27")
    args = parser.parse_args()
    project = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix="daily-news-build-") as temporary:
        root = Path(temporary)
        context, config = root / "context", root / "docker-config"
        context.mkdir()
        config.mkdir()
        (config / "config.json").write_text(
            json.dumps({"auths": {}, "proxies": {}}), encoding="utf-8"
        )
        prepare_context(project, context)
        environment = {**os.environ, "DOCKER_CONFIG": str(config)}
        if os.name == "nt" and not environment.get("DOCKER_HOST"):
            environment["DOCKER_HOST"] = "npipe:////./pipe/dockerDesktopLinuxEngine"
        for image, dockerfile, arguments in (
            ("daily-ai-news-tools:local", "docker/tool/Dockerfile", []),
            (
                "daily-ai-news-app:local",
                "docker/app/Dockerfile",
                [
                    "--build-arg",
                    "PYTHON_IMAGE=" + args.python_image,
                    "--build-arg",
                    "DOCKER_CLI_IMAGE=" + args.docker_cli_image,
                    "--build-arg",
                    "UV_IMAGE=" + args.uv_image,
                ],
            ),
        ):
            subprocess.run(
                [
                    "docker",
                    "build",
                    "--progress=plain",
                    "-t",
                    image,
                    "-f",
                    dockerfile,
                    *arguments,
                    ".",
                ],
                cwd=context,
                env=environment,
                check=True,
            )


if __name__ == "__main__":
    main()
