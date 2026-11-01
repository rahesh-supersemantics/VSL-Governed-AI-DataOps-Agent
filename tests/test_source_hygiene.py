"""Repository hygiene guards. A UTF-8 BOM at the start of a Python file made the AST-based static tests
(test_governance_static, test_recovery_static) fail with ``SyntaxError: invalid non-printable character U+FEFF``."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOM = b"\xef\xbb\xbf"
SOURCE_DIRS = ("app", "dashboard", "scripts", "tests", "config")
SKIP_PARTS = {"__pycache__", ".venv", "venv", "node_modules"}


def _files(*suffixes: str):
    for folder in SOURCE_DIRS:
        base = ROOT / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.suffix in suffixes and not (SKIP_PARTS & set(path.parts)):
                yield path


def test_no_python_source_file_starts_with_a_utf8_bom():
    offenders = [p.relative_to(ROOT).as_posix() for p in _files(".py") if p.read_bytes()[:3] == BOM]
    assert offenders == []


def test_no_yaml_config_file_starts_with_a_utf8_bom():
    offenders = [p.relative_to(ROOT).as_posix() for p in _files(".yaml", ".yml") if p.read_bytes()[:3] == BOM]
    assert offenders == []


def test_every_python_source_file_parses_as_utf8_text():
    for path in _files(".py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_pyyaml_is_declared_directly_in_requirements():
    names = {line.split("#")[0].strip().split("==")[0].lower() for line in (ROOT / "requirements.txt").read_text().splitlines()
             if line.strip() and not line.lstrip().startswith("#")}
    assert "pyyaml" in names


def test_generated_and_local_olist_artifacts_are_git_ignored():
    rules = {line.strip() for line in (ROOT / ".gitignore").read_text().splitlines()}
    for expected in ("docs/E-Commerce Dataset/", "claude_review_stage2/", "claude_review_stage2.zip", "reports/"):
        assert expected in rules, expected
    # existing rules are preserved
    for kept in (".env", "data/", ".dev/", "*.sqlite3"):
        assert kept in rules, kept
