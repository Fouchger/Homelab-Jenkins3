#!/usr/bin/env python3
"""Rotate the project-owned Proxmox API token and save it in Infisical."""

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


def request_json(url, method="GET", token=None, body=None, form=None, allow_404=False):
    headers = {"Accept": "application/json"}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if allow_404 and exc.code == 404:
            return None
        raise RuntimeError(f"HTTPS request failed with status {exc.code}; response content was suppressed") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"HTTPS request failed: {type(exc).__name__}") from None
    try:
        return json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("Infisical returned an invalid JSON response") from None


def infisical_login(base_url, client_id, client_secret):
    response = request_json(
        f"{base_url}/api/v1/auth/universal-auth/login",
        method="POST",
        form={"clientId": client_id, "clientSecret": client_secret},
    )
    access_token = response.get("accessToken") if isinstance(response, dict) else None
    if not access_token:
        raise RuntimeError("Infisical Universal Auth did not return an access token")
    return access_token


def secret_url(base_url, secret_name):
    query = urllib.parse.urlencode({
        "projectId": required("INFISICAL_PROJECT_ID"),
        "environment": required("INFISICAL_ENVIRONMENT"),
        "secretPath": "/proxmox/automation",
    })
    return f"{base_url}/api/v4/secrets/{urllib.parse.quote(secret_name)}?{query}"


def read_secret(base_url, bearer, name):
    result = request_json(secret_url(base_url, name), token=bearer, allow_404=True)
    if not result:
        return None
    secret = result.get("secret", {})
    value = secret.get("secretValue")
    if not isinstance(value, str):
        raise RuntimeError(f"Infisical secret {name} has no readable value")
    return value


def write_secret(base_url, bearer, name, value):
    url = secret_url(base_url, name)
    body = {
        "projectId": required("INFISICAL_PROJECT_ID"),
        "environment": required("INFISICAL_ENVIRONMENT"),
        "secretValue": value,
        "secretPath": "/proxmox/automation",
        "type": "shared",
        "skipMultilineEncoding": True,
    }
    method = "PATCH" if request_json(url, token=bearer, allow_404=True) else "POST"
    request_json(url, method=method, token=bearer, body=body)


def restore_secrets(base_url, bearer, old_id, old_secret):
    write_secret(base_url, bearer, "PVE_API_TOKEN_ID", old_id)
    write_secret(base_url, bearer, "PVE_API_TOKEN_SECRET", old_secret)


def shell_quote(value):
    return "'" + value.replace("'", "'\\''") + "'"


def ssh_script(host, script, private_key, known_hosts, timeout=90):
    with tempfile.TemporaryDirectory(prefix="pve-token-") as temp_dir:
        key_path = os.path.join(temp_dir, "id_ed25519")
        known_hosts_path = os.path.join(temp_dir, "known_hosts")
        for path, value in ((key_path, private_key.rstrip() + "\n"), (known_hosts_path, known_hosts.rstrip() + "\n")):
            with open(path, "w", encoding="utf-8") as output:
                output.write(value)
            os.chmod(path, 0o600)
        proc = subprocess.run(
            ["ssh", "-T", "-i", key_path, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
             "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts_path}",
             "-o", "ConnectTimeout=15", f"root@{host}", "bash", "-s"],
            input=script, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=timeout, check=False,
        )
    if proc.returncode:
        raise RuntimeError("Proxmox rejected the SSH or account command; output was suppressed")
    return proc.stdout


def create_remote_token(host, user, role, token_name, storage_ids, private_key, known_hosts):
    # The backing account keeps its existing ACL model. Privilege separation
    # additionally requires matching ACL entries for this specific token.
    acl_commands = [
        'pveum acl modify /vms -user "$user" -role "$role"',
        'pveum acl modify /vms -token "$full_id" -role "$role"',
    ]
    for storage in storage_ids:
        acl_commands.extend((
            f'pveum acl modify /storage/{storage} -user "$user" -role "$role"',
            f'pveum acl modify /storage/{storage} -token "$full_id" -role "$role"',
        ))
    acl_script = " && ".join(acl_commands)
    remote_script = f"""set -eu
user={shell_quote(user)}
role={shell_quote(role)}
token_name={shell_quote(token_name)}
full_id="$user!$token_name"
token_json=$(pveum user token add "$user" "$token_name" -privsep 1 --output-format json)
if ! {{
  {acl_script}
}}; then
  pveum user token remove "$user" "$token_name" >/dev/null 2>&1 || true
  exit 1
fi
printf '%s\\n' "$token_json"
"""
    output = ssh_script(host, remote_script, private_key, known_hosts)
    expected_id = f"{user}!{token_name}"
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        revoke_old_token(host, user, expected_id, private_key, known_hosts)
        raise RuntimeError("Proxmox did not return token JSON; token state needs manual review") from None
    secret = payload.get("value") or payload.get("token")
    full_id = payload.get("full-tokenid") or payload.get("fullTokenid") or expected_id
    if full_id != expected_id:
        revoke_old_token(host, user, expected_id, private_key, known_hosts)
        raise RuntimeError("Proxmox returned an unexpected token identifier; inspect token state before retrying")
    if not isinstance(secret, str) or not secret:
        revoke_old_token(host, user, full_id, private_key, known_hosts)
        raise RuntimeError("Proxmox created a token but did not return its secret; do not rerun blindly")
    return full_id, secret


def revoke_old_token(host, user, full_id, private_key, known_hosts):
    prefix = f"{user}!"
    if not full_id or not full_id.startswith(prefix):
        return False
    token_name = full_id[len(prefix):]
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", token_name):
        return False
    script = f"pveum user token remove {shell_quote(user)} {shell_quote(token_name)}\n"
    try:
        ssh_script(host, script, private_key, known_hosts, timeout=60)
        return True
    except (RuntimeError, subprocess.SubprocessError):
        return False


def rollback_new_token(host, user, full_id, previous_id, previous_secret, base_url,
                       write_token, private_key, known_hosts):
    """Restore prior Infisical values and revoke the uncommitted token."""
    try:
        restore_secrets(base_url, write_token, previous_id, previous_secret)
        restored = True
    except Exception:
        restored = False
    revoked = revoke_old_token(host, user, full_id, private_key, known_hosts)
    return restored, revoked


def main():
    base_url = required("INFISICAL_URL").rstrip("/")
    if not base_url.startswith("https://"):
        raise RuntimeError("Infisical URL must use HTTPS")
    read_token = infisical_login(base_url, required("INFISICAL_READ_CLIENT_ID"), required("INFISICAL_READ_CLIENT_SECRET"))
    write_token = infisical_login(base_url, required("INFISICAL_WRITE_CLIENT_ID"), required("INFISICAL_WRITE_CLIENT_SECRET"))
    private_key = read_secret(base_url, read_token, "PVE_SSH_PRIVATE_KEY")
    known_hosts = read_secret(base_url, read_token, "PVE_SSH_HOST_KEY")
    previous_id = read_secret(base_url, read_token, "PVE_API_TOKEN_ID") or ""
    previous_secret = read_secret(base_url, read_token, "PVE_API_TOKEN_SECRET") or ""
    if not private_key or not known_hosts:
        raise RuntimeError("/proxmox/automation must contain PVE_SSH_PRIVATE_KEY and PVE_SSH_HOST_KEY")
    if not previous_id or not previous_secret:
        raise RuntimeError("Both existing API token secrets must be present before rotation can start")

    host = required("PROXMOX_HOST")
    user = required("PROXMOX_USER")
    role = required("PROXMOX_ROLE")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        raise RuntimeError("PROXMOX_HOST must be a hostname or IP address")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+@pve", user):
        raise RuntimeError("PROXMOX_USER must be a project-owned account in the pve realm")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", role):
        raise RuntimeError("PROXMOX_ROLE must be an existing Proxmox role name")
    storage_ids = [item.strip() for item in os.environ.get("PROXMOX_STORAGE_IDS", "").split(",") if item.strip()]
    if not storage_ids or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", item) for item in storage_ids):
        raise RuntimeError("Provide comma-separated Proxmox storage IDs in PROXMOX_STORAGE_IDS")
    build_number = required("BUILD_NUMBER")
    if not build_number.isdecimal():
        raise RuntimeError("Jenkins BUILD_NUMBER must be numeric")
    token_name = f"jenkins-{build_number}"

    full_id, secret = create_remote_token(host, user, role, token_name, storage_ids, private_key, known_hosts)
    try:
        write_secret(base_url, write_token, "PVE_API_TOKEN_ID", full_id)
        write_secret(base_url, write_token, "PVE_API_TOKEN_SECRET", secret)
    except Exception:
        restored, revoked = rollback_new_token(host, user, full_id, previous_id, previous_secret,
                                               base_url, write_token, private_key, known_hosts)
        if not restored or not revoked:
            raise RuntimeError("Infisical update failed; rollback was incomplete. Verify the previous Infisical values and inspect the newly created Proxmox token before retrying") from None
        raise RuntimeError("Infisical update failed; restored previous values and removed the newly created Proxmox token") from None
    try:
        verified = (
            read_secret(base_url, write_token, "PVE_API_TOKEN_ID") == full_id
            and read_secret(base_url, write_token, "PVE_API_TOKEN_SECRET") == secret
        )
    except Exception:
        verified = False
    if not verified:
        restored, revoked = rollback_new_token(host, user, full_id, previous_id, previous_secret,
                                               base_url, write_token, private_key, known_hosts)
        if not restored or not revoked:
            raise RuntimeError("Infisical verification failed; rollback was incomplete. Verify the previous Infisical values and inspect the newly created Proxmox token before retrying")
        raise RuntimeError("Infisical verification failed; restored previous values and removed the newly created Proxmox token")

    if previous_id and previous_id != full_id:
        if revoke_old_token(host, user, previous_id, private_key, known_hosts):
            print("Saved and verified the new Infisical token; removed the previous token.")
        else:
            print("Saved and verified the new token, but could not remove the previous token; review it in Proxmox.")
    else:
        print("Saved and verified the new Proxmox API token in /proxmox/automation.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
