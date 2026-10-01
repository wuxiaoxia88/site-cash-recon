#!/usr/bin/env python3
"""Guard the public repository against leaking private site data.

Checks every tracked (or staged) text file for:
  * generic secrets: API-key/token assignments, JWTs, bearer tokens
  * personal data patterns: mainland mobile numbers, card-like digit runs,
    e-mail addresses (except example/noreply), private IPv4 addresses
  * a private denylist (site names, people, account codes...) read from
    ``$CASHRECON_HOME/leak-denylist.txt`` — that file never enters the repo.

A line can opt out with the marker ``leakcheck: allow``.

Usage: python scripts/leakcheck.py [--staged] [--denylist PATH]
Exit code 1 when findings exist.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ALLOW_MARKER = "leakcheck: allow"
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".pdf", ".zip"}

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("secret-assignment", re.compile(
        r"\b[A-Za-z0-9_]*(?:api[_-]?key|API[_-]?KEY|[Tt]oken|TOKEN|[Ss]ecret|SECRET|[Pp]assword|PASSWORD)"
        r"[A-Za-z0-9_]*\s*[=:]\s*['\"]?(?!<)(?![A-Z]+(?:_[A-Z0-9]+)+\b)[A-Za-z0-9_\-./+]{16,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.")),
    ("bearer", re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{20,}")),
    ("mobile", re.compile(r"(?<![\d.])1[3-9]\d{9}(?![\d.])")),
    ("card-number", re.compile(r"(?<![\d.])\d{16,19}(?![\d.])")),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@(?!example\.(com|org)\b)(?!users\.noreply\.github\.com\b)"
                         r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("private-ip", re.compile(r"\b(?:192\.168|10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b")),
]


def tracked_files(staged: bool) -> list[str]:
    if staged:
        out = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
                             capture_output=True, text=True, check=True).stdout
    else:
        out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line.strip()]


def read(path: str, staged: bool) -> str | None:
    if Path(path).suffix.lower() in SKIP_SUFFIXES:
        return None
    try:
        if staged:
            data = subprocess.run(["git", "show", f":{path}"], capture_output=True, check=True).stdout
        else:
            data = Path(path).read_bytes()
    except (OSError, subprocess.CalledProcessError):
        return None
    if b"\x00" in data[:4096]:
        return None
    return data.decode("utf-8", errors="replace")


def load_denylist(path: Path | None) -> list[str]:
    if path is None or not path.is_file():
        return []
    terms = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.append(line)
    return terms


def scan_text(name: str, text: str, denylist: list[str]) -> list[str]:
    findings = []
    deny = [(term, re.compile(r"(?<![\d])" + re.escape(term) + r"(?![\d])") if term.isdigit()
             else re.compile(re.escape(term), re.IGNORECASE)) for term in denylist]
    for number, line in enumerate(text.splitlines(), 1):
        if ALLOW_MARKER in line:
            continue
        for label, pattern in PATTERNS:
            if pattern.search(line):
                findings.append(f"{name}:{number}: {label}")
        for _term, pattern in deny:
            if pattern.search(line):
                findings.append(f"{name}:{number}: private-term (denylist)")
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--staged", action="store_true", help="scan the git index instead of the work tree")
    parser.add_argument("--denylist", type=Path, help="private denylist file")
    args = parser.parse_args(argv)
    home = Path(os.environ.get("CASHRECON_HOME") or Path.home() / ".site-cash-recon")
    denylist = load_denylist(args.denylist or home / "leak-denylist.txt")
    findings: list[str] = []
    for path in tracked_files(args.staged):
        if path == "scripts/leakcheck.py":
            continue
        text = read(path, args.staged)
        if text is not None:
            findings.extend(scan_text(path, text, denylist))
    if findings:
        print("leakcheck: possible private data found (values not shown):", file=sys.stderr)
        for item in findings:
            print("  " + item, file=sys.stderr)
        print(f"Add '{ALLOW_MARKER}' to a line only if it is definitely public.", file=sys.stderr)
        return 1
    print(f"leakcheck: ok ({'denylist active' if denylist else 'no private denylist'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
