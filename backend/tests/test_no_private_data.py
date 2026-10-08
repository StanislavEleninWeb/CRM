"""Guard for a public repository: nothing identifying from the private workbook may be committed."""

import os
import re
from pathlib import Path

import openpyxl
import pytest

REPO = Path(os.environ.get("REPO_ROOT", "/repo"))
SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "backups",
    ".pnpm-store",
    "__pycache__",
    ".data",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".claude",
}
SKIP_FILES = {"uv.lock", "pnpm-lock.yaml", "openapi.json", "schema.d.ts", ".env"}
TEXT_SUFFIXES = {
    ".py",
    ".ts",
    ".tsx",
    ".md",
    ".sh",
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".css",
    ".html",
    ".conf",
    ".mako",
    ".ini",
    ".txt",
    ".example",
}
IDENTIFYING_COLUMNS = {
    "Business name",
    "Website URL",
    "Google Business Profile / Maps URL",
    "Public business phone",
    "Public business email",
    "Contact page URL",
    "Other public business contact channel",
    "Evidence / relevant page URL",
}
# Shared infrastructure names that also occur in the workbook and identify nobody.
COMMON = {
    "google.com",
    "www.google.com",
    "maps.google.com",
    "facebook.com",
    "instagram.com",
    "gmail.com",
    "abv.bg",
    "mail.bg",
    "yahoo.com",
    "index.html",
}


def identifying_tokens(workbook: Path) -> dict[str, str]:
    tokens: dict[str, str] = {}

    def add(value: str, kind: str) -> None:
        value = value.strip().lower()
        if len(value) >= 7 and value not in COMMON:
            tokens.setdefault(value, kind)

    sheet = openpyxl.load_workbook(workbook, data_only=True, read_only=True).worksheets[0]
    rows = sheet.iter_rows(values_only=True)
    headers = next(rows)
    for row in rows:
        for header, value in zip(headers, row, strict=False):
            if not isinstance(value, str) or header not in IDENTIFYING_COLUMNS or value == "Not found":
                continue
            if header == "Business name":
                add(value, "name")
                for part in re.split(r"[()/]", value):
                    add(part, "name")
            for match in re.findall(r"[\w.+-]+@[\w.-]+\.\w+", value):
                add(match, "email")
            for match in re.findall(r"(?:https?://)?(?:www\.)?([a-z0-9-]+(?:\.[a-z0-9-]+)+)", value.lower()):
                add(match, "domain")
            for match in re.findall(r"place_id:([\w-]+)", value):
                add(match, "place_id")
            for match in re.findall(r"cid=(\d+)", value):
                add(match, "cid")
            for match in re.findall(r"(?:facebook|instagram)\.com/([\w.-]+)", value.lower()):
                add(match, "social")
            if header == "Public business phone":
                for match in re.findall(r"\+?\d[\d ]{6,}\d", value):
                    add(re.sub(r"\D", "", match)[-9:], "phone")
    return tokens


@pytest.mark.reference_fixture
def test_no_identifying_workbook_value_is_in_the_repository(reference_workbook: Path) -> None:
    if not (REPO / "AGENTS.md").exists():
        pytest.skip("repository root is not mounted")
    tokens = identifying_tokens(reference_workbook)
    assert len(tokens) > 500
    hits: list[tuple[str, str]] = []
    scanned = 0
    for directory, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not (d == "private" and directory.endswith("fixtures"))]
        for name in files:
            path = Path(directory) / name
            if name in SKIP_FILES or path.suffix not in TEXT_SUFFIXES:
                continue
            try:
                text = path.read_text(encoding="utf-8").lower()
            except (UnicodeDecodeError, OSError):
                continue
            scanned += 1
            digits = re.sub(r"[ \-]", "", text)
            for token, kind in tokens.items():
                if token in text or (kind == "phone" and token in digits):
                    hits.append((kind, str(path.relative_to(REPO))))
    assert scanned > 50
    assert hits == [], f"private workbook values found in: {sorted({where for _, where in hits})}"
