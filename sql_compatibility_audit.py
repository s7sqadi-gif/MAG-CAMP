"""Static PostgreSQL compatibility audit for MAG CAMP SQL literals.

Run before deployment:
    python sql_compatibility_audit.py
Returns non-zero when known SQLite-only constructs remain in production app.py.
"""
from __future__ import annotations
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TARGETS = [ROOT / "app.py", ROOT / "exit_feature.py"]
CHECKS = {
    "SQLite GROUP_CONCAT": re.compile(r"GROUP_CONCAT\s*\(", re.I),
    "SQLite julianday": re.compile(r"julianday\s*\(", re.I),
    "INSERT OR REPLACE": re.compile(r"INSERT\s+OR\s+REPLACE", re.I),
    "REPLACE INTO": re.compile(r"\bREPLACE\s+INTO\b", re.I),
}

issues: list[str] = []
for path in TARGETS:
    text = path.read_text(encoding="utf-8")
    for label, pattern in CHECKS.items():
        for match in pattern.finditer(text):
            # GROUP_CONCAT is allowed only in explicit SQLite branch.
            line_no = text.count("\n", 0, match.start()) + 1
            line = text.splitlines()[line_no - 1]
            if label == "SQLite GROUP_CONCAT" and "if IS_POSTGRES else" in line:
                continue
            issues.append(f"{path.name}:{line_no}: {label}: {line.strip()}")

# PostgreSQL requires ORDER BY expressions used with SELECT DISTINCT to appear in SELECT.
for path in TARGETS:
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        upper = line.upper()
        if "SELECT DISTINCT" in upper and "ORDER BY CAST(" in upper:
            select_part = upper.split("FROM", 1)[0]
            order_expr = upper.split("ORDER BY", 1)[1].split(")", 1)[0] + ")"
            if order_expr not in select_part:
                issues.append(f"{path.name}:{line_no}: DISTINCT/ORDER BY expression risk: {line.strip()}")

if issues:
    print("SQL COMPATIBILITY AUDIT: FAILED")
    print("\n".join(issues))
    raise SystemExit(1)
print("SQL COMPATIBILITY AUDIT: PASSED")
