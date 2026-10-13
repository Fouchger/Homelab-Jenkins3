#!/usr/bin/env python3
"""Run OpenTofu with Proxmox API credentials read from Infisical."""

import json
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required setting {name} is missing")
    return value


def request_json(url, method="GET", token=None, form=None):
    headers = {"Accept": "application/json"}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Infisical request failed with status {exc.code}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Infisical request failed: {type(exc).__name__}") from None
    try:
        result = json.loads(payload.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("Infisical returned invalid JSON") from None
    if not isinstance(result, dict):
        raise RuntimeError("Infisical returned an unexpected response")
    return result


def login(base_url):
    response = request_json(
        f"{base_url}/api/v1/auth/universal-auth/login",
        method="POST",
        form={
            "clientId": required("INFISICAL_READ_CLIENT_ID"),
            "clientSecret": required("INFISICAL_READ_CLIENT_SECRET"),
        },
    )
    token = response.get("accessToken")
    if not isinstance(token, str) or not token:
        raise RuntimeError("Infisical did not return a read access token")
    return token


def read_secret(base_url, token, name):
    query = urllib.parse.urlencode({
        "projectId": required("INFISICAL_PROJECT_ID"),
        "environment": required("INFISICAL_ENVIRONMENT"),
        "secretPath": "/proxmox/automation",
    })
    url = f"{base_url}/api/v4/secrets/{urllib.parse.quote(name)}?{query}"
    result = request_json(url, token=token)
    secret = result.get("secret", {})
    value = secret.get("secretValue") if isinstance(secret, dict) else None
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Infisical secret {name} is missing or unreadable")
    return value.strip()


def verify_proxmox_tls(host):
    url = f"https://{host}:8006/api2/json/version"
    try:
        with urllib.request.urlopen(url, timeout=10):
            pass
    except urllib.error.HTTPError as exc:
        # Proxmox normally requires authentication for this endpoint. Receiving
        # an HTTP response proves the TLS handshake and hostname check passed.
        if exc.code not in {401, 403}:
            raise RuntimeError(
                f"Proxmox API TLS preflight reached the endpoint but returned HTTP {exc.code}"
            ) from None
    except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise RuntimeError(
            "Proxmox API TLS preflight failed "
            f"({type(reason).__name__}). Set PROXMOX_HOST to a DNS name present in the "
            "certificate SAN and trust that certificate's issuing CA on the Jenkins agent."
        ) from None


def main():
    action = required("TOFU_ACTION")
    if action not in {"plan", "apply"}:
        raise RuntimeError("TOFU_ACTION must be plan or apply")

    project_root = Path(__file__).resolve().parents[3]
    environment_dir = project_root / "tofu" / "environments" / "kubernetes"
    tfvars_file = Path(required("HOMELAB_KUBERNETES_TFVARS"))
    plan_file = Path(required("TOFU_PLAN_FILE"))
    workspace = Path(required("WORKSPACE")).resolve()
    plan_file = plan_file.resolve()
    if workspace not in plan_file.parents:
        raise RuntimeError("The OpenTofu plan file must remain inside the Jenkins workspace")
    tfvars_file = tfvars_file.resolve()
    if workspace not in tfvars_file.parents or not tfvars_file.is_file():
        raise RuntimeError("The Jenkins cluster configuration file must exist inside the current workspace")
    os.chmod(tfvars_file, 0o600)

    state_dir = Path(required("HOMELAB_KUBERNETES_STATE_DIR"))
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_dir, 0o700)
    state_file = state_dir / "terraform.tfstate"

    host = required("PROXMOX_HOST")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        raise RuntimeError("PROXMOX_HOST must be a hostname or IPv4 address without a scheme")
    verify_proxmox_tls(host)

    infisical_url = required("INFISICAL_URL").rstrip("/")
    read_token = login(infisical_url)
    token_id = read_secret(infisical_url, read_token, "PVE_API_TOKEN_ID")
    token_secret = read_secret(infisical_url, read_token, "PVE_API_TOKEN_SECRET")
    if "!" not in token_id:
        raise RuntimeError("Infisical PVE_API_TOKEN_ID is not a fully qualified Proxmox token ID")

    env = os.environ.copy()
    env["PROXMOX_VE_ENDPOINT"] = f"https://{host}:8006/"
    env["PROXMOX_VE_API_TOKEN"] = f"{token_id}={token_secret}"

    subprocess.run([
        "tofu", "-chdir=" + str(environment_dir), "init", "-input=false",
        "-backend-config=path=" + str(state_file),
    ], env=env, check=True)
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    if action == "plan":
        command = [
            "tofu", "-chdir=" + str(environment_dir), "plan", "-input=false",
            "-var-file=" + str(tfvars_file), "-out=" + str(plan_file),
        ]
    else:
        if not plan_file.is_file():
            raise RuntimeError("The approved build plan is missing from this Jenkins workspace")
        command = ["tofu", "-chdir=" + str(environment_dir), "apply", "-input=false", str(plan_file)]
    subprocess.run(command, env=env, check=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        message = str(exc) if isinstance(exc, RuntimeError) else f"OpenTofu execution failed ({type(exc).__name__})"
        print(message, file=sys.stderr)
        sys.exit(1)
