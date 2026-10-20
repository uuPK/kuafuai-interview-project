"""Create a local configuration once, without replacing existing credentials."""

import argparse
import os
import secrets
from pathlib import Path


def prepare(directory):
    directory = Path(directory)
    target = directory / ".env"
    if target.exists():
        return False
    template = (directory / ".env.example").read_text(encoding="utf-8")
    if "SESSION_SECRET=\n" not in template:
        raise ValueError("template requires an empty SESSION_SECRET field")
    content = template.replace(
        "SESSION_SECRET=\n", "SESSION_SECRET=" + secrets.token_urlsafe(48) + "\n", 1
    )
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(content)
    except FileExistsError:
        return False
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Create .env with a unique session secret")
    parser.add_argument("--directory", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args(argv)
    try:
        created = prepare(args.directory)
    except (OSError, ValueError):
        parser.exit(1, "Cannot create .env; check .env.example and directory permissions.\n")
    print(
        "Created .env. Fill in your DeepSeek and Bocha settings before generating news."
        if created
        else "Kept existing .env unchanged."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
