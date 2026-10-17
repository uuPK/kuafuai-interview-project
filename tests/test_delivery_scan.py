import json

from scripts.check_delivery import inspect_files


def test_scan_catches_configured_secret_and_runtime_files_without_echoing(tmp_path):
    (tmp_path / "config.py").write_text("value='fixture-secret-42'", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data/private.json").write_text("private", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=local", encoding="utf-8")
    report = inspect_files(
        tmp_path, ["config.py", "data/private.json", ".env"], ["fixture-secret-42"]
    )
    assert not report["ok"]
    assert report["secret_matches"] == ["config.py"]
    assert report["forbidden_tracked_files"] == ["data/private.json", ".env"]
    assert "fixture-secret-42" not in json.dumps(report)


def test_scan_allows_templates_and_does_not_follow_outside_paths(tmp_path):
    (tmp_path / ".env.example").write_text("KEY=", encoding="utf-8")
    report = inspect_files(tmp_path, [".env.example"], ["actual-secret"])
    assert report["ok"] and report["files_scanned"] == 1
    report = inspect_files(tmp_path, ["../outside", "missing"], [])
    assert not report["ok"] and report["files_scanned"] == 0
