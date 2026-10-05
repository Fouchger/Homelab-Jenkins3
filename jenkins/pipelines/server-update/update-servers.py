#!/usr/bin/env python3
"""Trigger the existing Proxmox bootstrap to update both Jenkins LXCs."""

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required setting {name} is missing")
    return value


def http_json(url, method="GET", headers=None, form=None):
    request_headers = {"Accept": "application/json"}
    request_headers.update(headers or {})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Infisical request failed with HTTP {exc.code}; response was suppressed") from None
    except (urllib.error.URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Infisical request failed: {type(exc).__name__}") from None


def fetch_proxmox_host_key():
    base_url = required("INFISICAL_URL").rstrip("/")
    if not base_url.startswith("https://"):
        raise RuntimeError("INFISICAL_URL must use HTTPS")
    login = http_json(
        f"{base_url}/api/v1/auth/universal-auth/login",
        method="POST",
        form={
            "clientId": required("INFISICAL_READ_CLIENT_ID"),
            "clientSecret": required("INFISICAL_READ_CLIENT_SECRET"),
        },
    )
    access_token = login.get("accessToken") if isinstance(login, dict) else None
    if not access_token:
        raise RuntimeError("Infisical did not return an access token")
    query = urllib.parse.urlencode({
        "projectId": required("INFISICAL_PROJECT_ID"),
        "environment": required("INFISICAL_ENVIRONMENT"),
        "secretPath": "/proxmox/automation",
    })
    result = http_json(
        f"{base_url}/api/v4/secrets/PVE_SSH_HOST_KEY?{query}",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    value = result.get("secret", {}).get("secretValue") if isinstance(result, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("Infisical secret /proxmox/automation/PVE_SSH_HOST_KEY is missing or empty")
    return value.strip() + "\n"


def run_ssh(host, key_file, known_hosts_file, remote_command, input_text=None, capture=False, timeout=120):
    command = [
        "ssh", "-T", "-i", key_file,
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={known_hosts_file}",
        "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        f"root@{host}", remote_command,
    ]
    result = subprocess.run(
        command,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        if capture:
            raise RuntimeError("SSH setup command failed; host output was suppressed")
        raise RuntimeError(f"Proxmox update exited with status {result.returncode}")
    return result.stdout.strip() if capture else ""


def main():
    if required("PROXMOX_SSH_USER") != "root":
        raise RuntimeError("The pve01-automation-ssh credential must connect as root for pct operations")
    host = required("PROXMOX_HOST")
    owner = required("GITHUB_OWNER")
    repository = required("GITHUB_REPOSITORY")
    commit = required("UPDATE_COMMIT")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        raise RuntimeError("PROXMOX_HOST must be a hostname or IPv4 address")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repository):
        raise RuntimeError("Invalid GitHub repository owner or name")
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", commit):
        raise RuntimeError("UPDATE_COMMIT must be the checked-out Git commit SHA")

    known_hosts = fetch_proxmox_host_key()
    key_file = required("PROXMOX_SSH_KEY_FILE")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="pve-known-hosts-", delete=False) as host_file:
        host_file.write(known_hosts)
        known_hosts_file = host_file.name
    os.chmod(known_hosts_file, 0o600)
    remote_dir = ""
    try:
        remote_dir = run_ssh(
            host, key_file, known_hosts_file,
            "umask 077; mktemp -d /tmp/homelab-jenkins-update.XXXXXX",
            capture=True,
        )
        if not re.fullmatch(r"/tmp/homelab-jenkins-update\.[A-Za-z0-9]{6}", remote_dir):
            raise RuntimeError("Proxmox returned an unexpected temporary directory")

        script = f"""set -Eeuo pipefail
update_dir='{remote_dir}'
cleanup_update_dir() {{ rm -rf -- "$update_dir"; }}
trap cleanup_update_dir EXIT
curl --fail --silent --show-error --location \\
  --retry 3 --connect-timeout 15 --max-time 120 \\
  'https://raw.githubusercontent.com/{owner}/{repository}/{commit}/proxmox_helper_script/controlplane.sh' \\
  -o "$update_dir/controlplane.sh"
[[ -s $update_dir/controlplane.sh ]]
bash -n "$update_dir/controlplane.sh"
chmod 0700 "$update_dir/controlplane.sh"
printf 'Running repository update for commit {commit}; both LXCs are explicitly set to reuse.\\n'
HOMELAB_BOOTSTRAP_OWNER='{owner}' \\
HOMELAB_BOOTSTRAP_REPOSITORY='{repository}' \\
HOMELAB_BOOTSTRAP_REF='{commit}' \\
HOMELAB_CONTROLPLANE_ACTION=reuse \\
HOMELAB_AGENT_ACTION=reuse \\
HOMELAB_DEFER_AGENT_RESTART=yes \\
HOMELAB_SKIP_AGENT_ENROLMENT=yes \\
  bash "$update_dir/controlplane.sh"
"""
        run_ssh(host, key_file, known_hosts_file, "bash -s", input_text=script, timeout=5400)
        print(f"Proxmox bootstrap completed for commit {commit}.")
    finally:
        if remote_dir:
            try:
                run_ssh(host, key_file, known_hosts_file, f"rm -rf -- {remote_dir}", timeout=30)
            except (RuntimeError, subprocess.SubprocessError):
                print("WARNING: remote temporary update directory cleanup failed; remove it on Proxmox.", file=sys.stderr)
        try:
            os.unlink(known_hosts_file)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
