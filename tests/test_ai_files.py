"""The files written for AI tools (Agent Skill, llms.txt, context7.json) stay valid and in sync."""

import json
import re
from pathlib import Path

import uncapped

ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = ROOT / "skills" / "uncapped"
RAW = "https://raw.githubusercontent.com/bhatti013/uncapped/main/"
BLOB = "https://github.com/bhatti013/uncapped/blob/main/"


def frontmatter(path):
    """Read the simple YAML frontmatter used by SKILL.md (top-level keys and one nested map)."""
    text = path.read_text()
    assert text.startswith("---\n"), "SKILL.md must start with YAML frontmatter"
    head, body = text[4:].split("\n---\n", 1)
    fields, parent = {}, None
    for line in head.splitlines():
        if line.startswith("  "):
            key, _, value = line.strip().partition(":")
            fields[parent][key] = value.strip().strip('"')
        else:
            key, _, value = line.partition(":")
            parent = key
            fields[key] = value.strip() or {}
    return fields, body


def outside_code(text):
    """Lines of a markdown file that are not inside fenced code blocks."""
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            yield line


def linked_repo_files(text):
    for url in re.findall(r"\((https://[^)\s]+)\)", text):
        for prefix in (RAW, BLOB):
            if url.startswith(prefix):
                yield url[len(prefix):]


def test_skill_frontmatter_follows_the_agent_skills_spec():
    fields, body = frontmatter(SKILL_DIR / "SKILL.md")
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", fields["name"]) and len(fields["name"]) <= 64
    assert fields["name"] == SKILL_DIR.name
    assert 0 < len(fields["description"]) <= 1024
    assert len(fields["compatibility"]) <= 500
    assert len(body.splitlines()) < 500


def test_skill_version_matches_the_package():
    fields, _ = frontmatter(SKILL_DIR / "SKILL.md")
    assert fields["metadata"]["version"] == uncapped.__version__


def test_skill_relative_links_exist():
    for path in SKILL_DIR.rglob("*.md"):
        for target in re.findall(r"\]\(([^)#\s]+)\)", path.read_text()):
            if not target.startswith(("http://", "https://")):
                assert (path.parent / target).exists(), f"{path.name} links to missing {target}"


def test_llms_txt_follows_the_format():
    text = (ROOT / "llms.txt").read_text()
    lines = [line for line in outside_code(text) if line.strip()]
    assert lines[0] == "# uncapped"
    assert lines[1].startswith("> ")
    headings = [line for line in lines[1:] if line.startswith("#")]
    assert all(h.startswith("## ") for h in headings), "only H2 sections after the title"
    in_lists = False
    for line in lines:
        if line.startswith("## "):
            in_lists = True
        elif in_lists:
            assert re.match(r"- \[[^\]]+\]\(https?://[^)]+\)", line), f"not a file-list entry: {line}"


def test_links_point_to_files_that_exist():
    for name in ("llms.txt", "README.md", "skills/uncapped/SKILL.md"):
        for rel in linked_repo_files((ROOT / name).read_text()):
            assert (ROOT / rel).exists(), f"{name} links to missing {rel}"


def test_context7_config_is_valid():
    config = json.loads((ROOT / "context7.json").read_text())
    assert config["projectTitle"] == "uncapped"
    assert len(config["projectTitle"]) <= 100
    assert 0 < len(config["description"]) <= 500
    assert config["rules"] and all(isinstance(rule, str) and rule for rule in config["rules"])
