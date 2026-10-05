#!/usr/bin/env python3
"""Verify the Infisical Universal Auth credentials without exposing secrets."""

import os
import json
import sys
import urllib.error
import urllib.parse
import urllib.request


def authenticate(base_url, label, client_id_name, client_secret_name):
    client_id = os.environ.get(client_id_name, "").strip()
    client_secret = os.environ.get(client_secret_name, "").strip()
    if not client_id or not client_secret:
        raise RuntimeError(f"The {label} Infisical credential is empty")

    request = urllib.request.Request(
        f"{base_url}/api/v1/auth/universal-auth/login",
        data=urllib.parse.urlencode({"clientId": client_id, "clientSecret": client_secret}).encode(),
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
            if response.status != 200 or not result.get("accessToken"):
                raise RuntimeError(f"The {label} Infisical identity login failed (HTTP {response.status})")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"The {label} Infisical identity login failed (HTTP {exc.code}); response suppressed") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"The {label} Infisical identity could not connect ({type(exc).__name__})") from None


def main():
    base_url = os.environ.get("INFISICAL_URL", "https://app.infisical.com").strip().rstrip("/")
    if not base_url.startswith("https://"):
        raise RuntimeError("INFISICAL_URL must use HTTPS")

    authenticate(base_url, "read-only", "INFISICAL_READ_CLIENT_ID", "INFISICAL_READ_CLIENT_SECRET")
    print("Infisical read-only Machine Identity authenticated successfully.")
    authenticate(base_url, "read/write", "INFISICAL_WRITE_CLIENT_ID", "INFISICAL_WRITE_CLIENT_SECRET")
    print("Infisical read/write Machine Identity authenticated successfully.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
