"""Saved research data survives every supported additive schema migration."""
from scripts.validate_install import seed_legacy, snapshot, verify_legacy


def test_legacy_workspace_upgrade_preserves_rows_files_and_repeated_startup(tmp_path):
    data = tmp_path / "legacy"
    fixture = seed_legacy(data)
    before = snapshot(data / "research.sqlite3")
    report = verify_legacy(data, before, fixture)
    assert report["passed"] and report["idempotent"]
    assert report["preserved_rows"]["note_versions"] == 2
    assert report["preserved_rows"]["chat_sessions"] == 1
    assert report["preserved_rows"]["translations"] == 1
