import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_codex_plugin_manifest_points_at_shared_skills():
    manifest = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))

    assert manifest["name"] == "teams-video-dl"
    assert manifest["skills"] == "./skills/"
    assert manifest["interface"]["displayName"] == "Teams Video Downloader"
    assert manifest["interface"]["defaultPrompt"] == [
        "Download this Teams recording with its transcript: <url>"
    ]


def test_codex_repo_marketplace_points_at_plugin_root_without_copying_skills():
    marketplace = json.loads((ROOT / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8"))

    entry = next(plugin for plugin in marketplace["plugins"] if plugin["name"] == "teams-video-dl")
    assert entry["source"] == {"source": "local", "path": "./"}
    assert entry["policy"] == {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}
    assert entry["category"] == "Productivity"


def test_claude_plugin_marketplace_points_at_shared_skill_root():
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))

    assert manifest["name"] == "teams-video-dl"
    assert manifest["description"]
    entry = next(plugin for plugin in marketplace["plugins"] if plugin["name"] == "teams-video-dl")
    assert entry["source"] == "./"
    assert (ROOT / "skills" / "teams-video-dl" / "SKILL.md").exists()


def test_shared_skill_uses_agent_neutral_skill_dir_placeholder():
    skill = (ROOT / "skills" / "teams-video-dl" / "SKILL.md").read_text(encoding="utf-8")

    assert "CLAUDE_SKILL_DIR" not in skill
    assert not re.search(r"^argument-hint:", skill, re.MULTILINE)
    assert re.search(r"^metadata:\n  argument-hint:", skill, re.MULTILINE)
    assert '<SKILL_DIR>/scripts/' not in skill
    assert '"$SKILL_DIR/scripts/har_extract.py"' in skill


def test_publication_versions_are_in_sync():
    codex_manifest = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    claude_manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    claude_marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))

    assert codex_manifest["version"] == "1.0.3"
    assert claude_manifest["version"] == codex_manifest["version"]
    assert claude_marketplace["plugins"][0]["version"] == codex_manifest["version"]


def test_skill_uses_destination_local_tmp_workspace():
    skill = (ROOT / "skills" / "teams-video-dl" / "SKILL.md").read_text(encoding="utf-8")

    assert 'TMP_DIR="$DEST_DIR/tmp"' in skill
    assert 'uv --cache-dir "$TMP_DIR/uv-cache" run' in skill
    assert '--out-dir "$TMP_DIR"' in skill
    assert '--work-dir "$TMP_DIR/stream"' in skill
    assert 'rm -rf "$TMP_DIR"' in skill


def test_gitignore_excludes_destination_local_tmp_workspace():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "tmp/" in gitignore


def test_skill_cleanup_retries_and_checks_tmp_credentials():
    skill = (ROOT / "skills" / "teams-video-dl" / "SKILL.md").read_text(encoding="utf-8")

    assert 'find "$TMP_DIR" -name .DS_Store -delete' in skill
    assert 'sleep 1' in skill
    assert 'WARNING: temp cleanup incomplete' in skill


def test_repository_fixtures_do_not_contain_sensitive_recording_title():
    forbidden = tuple(
        "".join(chr(codepoint) for codepoint in codepoints)
        for codepoints in (
            (65, 49, 50),
            (109, 103, 109),
            (65, 114, 99, 104, 105, 116, 101, 99, 116, 117, 114, 101, 32, 69, 120, 99, 104, 97, 110, 103, 101),
            (65, 114, 99, 104, 105, 116, 101, 99, 116, 117, 114, 101, 95, 69, 120, 99, 104, 97, 110, 103, 101),
        )
    )
    checked_suffixes = {".md", ".py", ".json"}
    ignored_parts = {".git", ".uv-cache", ".pytest_cache", "__pycache__"}

    for path in ROOT.rglob("*"):
        if ignored_parts.intersection(path.parts) or path.is_dir() or path.suffix not in checked_suffixes:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for phrase in forbidden:
            assert phrase not in text, f"{phrase!r} found in {path.relative_to(ROOT)}"
