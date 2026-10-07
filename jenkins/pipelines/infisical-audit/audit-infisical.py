#!/usr/bin/env python3
"""Inventory Infisical secret names without retrieving or displaying values."""

import csv
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[3]
EXPECTED_FILE = Path(__file__).with_name("expected-secrets.json")
ARTIFACT_DIR = ROOT / "artifacts"


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required Jenkins setting {name} is missing")
    return value


def request_json(url, *, method="GET", headers=None, form=None):
    request_headers = {"Accept": "application/json"}
    request_headers.update(headers or {})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Infisical request failed with HTTP {exc.code}; response was suppressed") from None
    except (urllib.error.URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Infisical request failed: {type(exc).__name__}") from None


def fetch_inventory():
    base = required("INFISICAL_URL").rstrip("/")
    if not base.startswith("https://"):
        raise RuntimeError("INFISICAL_URL must use HTTPS")
    login = request_json(
        f"{base}/api/v1/auth/universal-auth/login",
        method="POST",
        form={
            "clientId": required("INFISICAL_READ_CLIENT_ID"),
            "clientSecret": required("INFISICAL_READ_CLIENT_SECRET"),
        },
    )
    token = login.get("accessToken") if isinstance(login, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError("Infisical read identity did not authenticate")
    query = urllib.parse.urlencode({
        "projectId": required("INFISICAL_PROJECT_ID"),
        "environment": required("INFISICAL_ENVIRONMENT"),
        "secretPath": "/",
        "recursive": "true",
        "viewSecretValue": "false",
        "includeImports": "false",
    })
    response = request_json(
        f"{base}/api/v4/secrets?{query}",
        headers={"Authorization": f"Bearer {token}"},
    )
    entries = response.get("secrets") if isinstance(response, dict) else None
    if not isinstance(entries, list):
        raise RuntimeError("Infisical returned an invalid secret-name inventory")
    inventory = set()
    for item in entries:
        if not isinstance(item, dict):
            continue
        name = item.get("secretKey")
        path = item.get("secretPath")
        if isinstance(name, str) and name and isinstance(path, str):
            inventory.add((path.rstrip("/") or "/", name))
    return inventory


def build_report(actual, expected):
    expected_keys = set()
    rows = []
    failed = False
    for item in expected:
        key = (item["path"], item["name"])
        expected_keys.add(key)
        present = key in actual
        state = item["state"]
        status = "SET" if present else (
            "MISSING" if state in ("required", "required-for-reset") else
            "MISSING-CONDITIONAL" if state == "conditional" else
            "OPTIONAL-MISSING" if state == "optional" else
            "NOT-YET-CREATED" if state in ("created", "bootstrap") else
            "PLANNED"
        )
        if not present and state in ("required", "required-for-reset"):
            failed = True
        rows.append({
            "status": status,
            "folder": item["path"],
            "variable": item["name"],
            "expected_state": state,
            "pipeline_use": item["used_by"],
        })
    for path, name in sorted(actual - expected_keys):
        rows.append({
            "status": "UNTRACKED",
            "folder": path,
            "variable": name,
            "expected_state": "review",
            "pipeline_use": "Not in the repository's declared Infisical inventory",
        })
    return rows, failed


def main():
    actual = fetch_inventory()
    expected = json.loads(EXPECTED_FILE.read_text(encoding="utf-8"))
    if not isinstance(expected, list):
        raise RuntimeError("Expected Infisical inventory definition must be a JSON list")
    rows, missing_required = build_report(actual, expected)
    ARTIFACT_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(ARTIFACT_DIR, 0o700)
    build = required("BUILD_NUMBER")
    if not build.isdecimal():
        raise RuntimeError("Jenkins BUILD_NUMBER is invalid")
    report = ARTIFACT_DIR / f"infisical-audit-{build}.csv"
    with report.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=("status", "folder", "variable", "expected_state", "pipeline_use"))
        writer.writeheader()
        writer.writerows(rows)
    os.chmod(report, 0o600)

    print("Infisical variable audit (names only; secret values were not requested or saved):")
    print(f"{'STATUS':<18} {'FOLDER':<38} VARIABLE")
    print("-" * 100)
    for row in rows:
        print(f"{row['status']:<18} {row['folder']:<38} {row['variable']}")
    print(f"\nInventory contains {len(actual)} configured variable(s); report saved as {report.relative_to(ROOT)}.")
    if missing_required:
        print("One or more required variables are missing. See MISSING rows above.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
