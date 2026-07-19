#!/usr/bin/env python3
"""scripts/secret_scan.py — secret scanner for staged diffs, archives, and files.

Detects likely real credentials. Prints only redacted matches.
Exits non-zero when a likely real secret is found.

Uses shared pattern definitions from scripts/secret_patterns.py so results
are consistent with scripts/verify_source_archive.py.

Usage:
    python scripts/secret_scan.py --staged
    python scripts/secret_scan.py --archive path/to/code.zip
    python scripts/secret_scan.py --files .env.example
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import zipfile
from pathlib import Path

# Shared patterns — single source of truth for all hardening tools.
from secret_patterns import (
    BINARY_EXTENSIONS,
    Finding,
    scan_file,
    scan_text,
)


def _print_findings(findings: list[Finding]) -> None:
    for f in findings:
        loc = f"{f.source}" + (f":{f.line_no}" if f.line_no else "")
        print(f"  FINDING [{f.pattern_name}] at {loc} — value: {f.redacted}")


def scan_staged() -> list[Finding]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--unified=0"],
        capture_output=True,
    )
    if result.returncode != 0:
        print("WARNING: could not get staged diff", file=sys.stderr)
        return []

    # Explicit UTF-8 decode with errors="replace" so Bengali or other non-ASCII
    # characters in staged files do not crash the scanner on Windows cp1252.
    diff_text = result.stdout.decode("utf-8", errors="replace")

    findings: list[Finding] = []
    current_file = "staged"
    line_no = 0

    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
            line_no = 0
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            line_no = int(m.group(1)) if m else 0
        elif line.startswith("+") and not line.startswith("+++"):
            line_no += 1
            content = line[1:]
            for f in scan_text(content, f"{current_file}:{line_no}"):
                findings.append(Finding(f"{current_file}", line_no, f.pattern_name, f.redacted))

    return findings


def scan_archive(archive_path: Path) -> list[Finding]:
    findings: list[Finding] = []
    with zipfile.ZipFile(archive_path) as zf:
        for entry in zf.infolist():
            name = entry.filename
            ext = Path(name).suffix.lower()
            if ext in BINARY_EXTENSIONS:
                continue
            # Documentation files (.md, .rst, .txt) ARE scanned — they can
            # contain real credentials (runbooks, READMEs with embedded tokens).
            # Individual safe lines may use the allow-pattern-definition marker.
            try:
                text = zf.read(name).decode("utf-8", errors="replace")
            except Exception:
                continue
            for f in scan_text(text, f"archive:{name}"):
                findings.append(f)
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Secret scanner — never prints full values")
    parser.add_argument("--staged",  action="store_true", help="Scan staged git diff")
    parser.add_argument("--archive", type=Path,           help="Scan a .zip source archive")
    parser.add_argument("--files",   nargs="+", type=Path, help="Scan specific files")
    args = parser.parse_args()

    if not (args.staged or args.archive or args.files):
        parser.print_help()
        return 0

    all_findings: list[Finding] = []

    if args.staged:
        print("Scanning staged diff...")
        found = scan_staged()
        if found:
            print(f"  {len(found)} finding(s) in staged content:")
            _print_findings(found)
        else:
            print("  Staged diff: clean.")
        all_findings.extend(found)

    if args.archive:
        print(f"Scanning archive: {args.archive} ...")
        found = scan_archive(args.archive)
        if found:
            print(f"  {len(found)} finding(s) in archive:")
            _print_findings(found)
        else:
            print("  Archive: clean.")
        all_findings.extend(found)

    if args.files:
        for f in args.files:
            print(f"Scanning file: {f} ...")
            found = scan_file(f)
            if found:
                print(f"  {len(found)} finding(s):")
                _print_findings(found)
            else:
                print("  Clean.")
            all_findings.extend(found)

    if all_findings:
        print(f"\nFAIL — {len(all_findings)} likely secret(s) found. See findings above (values redacted).")
        return 1
    print("\nPASS — no likely secrets found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
