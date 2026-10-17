"""Check only explicitly tracked project files; never print configured secret values."""

import json
import subprocess
from pathlib import Path

from pydantic import SecretStr

from app.config import Settings


def inspect_files(root, files, secrets):
    forbidden, leaks, checked = [], [], 0
    root = root.resolve()
    for name in files:
        path = root / name
        parts = Path(name).parts
        if (
            not parts
            or path.is_symlink()
            or not path.resolve().is_relative_to(root)
            or not path.is_file()
        ):
            forbidden.append(name)
            continue
        if (
            parts[0] == "data"
            or (path.name.startswith(".env") and not path.name.endswith(".example"))
            or path.suffix in {".sqlite3", ".pem", ".key"}
            or path.name.endswith((".sqlite3-wal", ".sqlite3-shm"))
        ):
            forbidden.append(name)
        checked += 1
        content = path.read_bytes()
        if any(value.encode("utf-8") in content for value in secrets if value):
            leaks.append(name)
    return {
        "files_scanned": checked,
        "configured_secret_values": len(secrets),
        "forbidden_tracked_files": forbidden,
        "secret_matches": leaks,
        "ok": not (forbidden or leaks),
    }


def main():
    root = Path(__file__).resolve().parent.parent
    files = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode("utf-8").split("\0")
    settings = Settings()
    secrets = [
        value.get_secret_value()
        for name in Settings.model_fields
        if isinstance(value := getattr(settings, name), SecretStr)
    ]
    report = inspect_files(root, [name for name in files if name], secrets)
    target = root / "data/acceptance/delivery-secret-scan.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
