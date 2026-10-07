#!/usr/bin/env python3
"""Store the generated Proxmox SSH identity in Infisical and authorize it locally."""

import json
import os
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class SetupError(RuntimeError):
    pass


def read_file(folder, name, *, required=True):
    path = Path(folder, name)
    try:
        value = path.read_text(encoding="utf-8").rstrip("\r\n")
    except OSError:
        if required:
            raise SetupError(f"Required setup input is missing: {name}") from None
        return ""
    if required and not value:
        raise SetupError(f"Required setup input is empty: {name}")
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
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        if allow_404 and exc.code == 404:
            return None
        raise SetupError(f"Infisical request failed with HTTP {exc.code}; response was suppressed") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SetupError(f"Infisical request failed: {type(exc).__name__}") from None
    if not payload:
        return {}
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SetupError("Infisical returned an invalid response") from None


def login(base_url, client_id, client_secret, label):
    response = request_json(
        f"{base_url}/api/v1/auth/universal-auth/login",
        method="POST",
        form={"clientId": client_id, "clientSecret": client_secret},
    )
    token = response.get("accessToken") if isinstance(response, dict) else None
    if not isinstance(token, str) or not token:
        raise SetupError(f"Infisical {label} identity did not authenticate")
    return token


def public_key_pair(line):
    fields = line.split()
    for index, field in enumerate(fields[:-1]):
        if field == "ssh-ed25519" and index + 1 < len(fields):
            return field, fields[index + 1], fields[-1]
    return None


def update_authorized_keys(public_key, prune=False):
    fields = public_key.split()
    marker = fields[-1]
    if not marker.startswith("homelab-jenkins-update-"):
        raise SetupError("Generated SSH key comment is invalid")
    ssh_dir = Path("/root/.ssh")
    if ssh_dir.is_symlink():
        raise SetupError("/root/.ssh must not be a symbolic link")
    ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(ssh_dir, 0o700)
    authorized_keys = ssh_dir / "authorized_keys"
    if authorized_keys.is_symlink():
        # Proxmox links root's authorized_keys to the cluster-managed file.
        # Follow only that exact, expected target; never replace the symlink.
        expected_target = Path("/etc/pve/priv/authorized_keys")
        try:
            resolved_target = authorized_keys.resolve(strict=True)
        except (OSError, RuntimeError):
            raise SetupError("Proxmox root authorized_keys link cannot be resolved") from None
        if resolved_target != expected_target:
            raise SetupError("Proxmox root authorized_keys link has an unexpected target")
        authorized_keys = resolved_target
    if authorized_keys.exists() and not stat.S_ISREG(authorized_keys.stat().st_mode):
        raise SetupError("Proxmox authorized_keys target must be a regular file")
    existing_lines = authorized_keys.read_text(encoding="utf-8").splitlines() if authorized_keys.exists() else []
    retained = []
    key_exists = False
    for line in existing_lines:
        pair = public_key_pair(line)
        if pair and pair[2].startswith("homelab-jenkins-update"):
            if prune:
                if pair[:2] == tuple(fields[:2]):
                    if not key_exists:
                        retained.append(line)
                        key_exists = True
                continue
            if pair[:2] == tuple(fields[:2]):
                key_exists = True
        retained.append(line)
    if not key_exists:
        retained.append(public_key)
    temp_path = authorized_keys.with_name(authorized_keys.name + ".homelab-tmp")
    if temp_path.is_symlink():
        raise SetupError("Temporary authorized_keys path must not be a symbolic link")
    temp_path.write_text("\n".join(retained) + "\n", encoding="utf-8")
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, authorized_keys)


def save_lxc_password(folder, role, password_file):
    if role not in ("controlplane", "jenkins-agent", "dns01", "dns02"):
        raise SetupError("Unsupported LXC role for root-password storage")
    base_url = read_file(folder, "infisical-url").rstrip("/")
    project_id = read_file(folder, "project-id")
    environment = read_file(folder, "environment")
    client_id = read_file(folder, "write-client-id")
    client_secret = read_file(folder, "write-client-secret")
    password = Path(password_file).read_text(encoding="utf-8").rstrip("\r\n")
    if not password:
        raise SetupError("LXC root password is empty")
    if not base_url.startswith("https://"):
        raise SetupError("Infisical URL must be an HTTPS URL")
    if not re.fullmatch(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", project_id):
        raise SetupError("Infisical project ID must be a UUID")
    token = login(base_url, client_id, client_secret, "read/write")
    secret_path = f"/proxmox/lxc/{role}"
    query = urllib.parse.urlencode({
        "projectId": project_id,
        "environment": environment,
        "secretPath": secret_path,
    })
    url = f"{base_url}/api/v4/secrets/LXC_ROOT_PASSWORD?{query}"
    body = {
        "projectId": project_id,
        "environment": environment,
        "secretValue": password,
        "secretPath": secret_path,
        "type": "shared",
        "skipMultilineEncoding": True,
    }
    existing = request_json(url, token=token, allow_404=True)
    method = "PATCH" if existing else "POST"
    request_json(url, method=method, token=token, body=body)
    verified = request_json(url, token=token)
    value = verified.get("secret", {}).get("secretValue") if isinstance(verified, dict) else None
    if value != password:
        raise SetupError(f"Infisical could not verify LXC_ROOT_PASSWORD for {role}")
    print(f"Stored and verified LXC_ROOT_PASSWORD at {secret_path} in Infisical.")


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--prune-authorized-keys":
        public_key = read_file(sys.argv[2], "pve-public-key")
        update_authorized_keys(public_key, prune=True)
        print("Removed superseded managed SSH keys from Proxmox; the current key remains authorized.")
        return
    if len(sys.argv) == 4 and sys.argv[1] == "--save-lxc-password":
        save_lxc_password(sys.argv[2], sys.argv[3], os.path.join(sys.argv[2], f"root-password-{sys.argv[3]}"))
        return
    if len(sys.argv) != 3:
        raise SetupError("Usage: configure-infisical.py <protected-setup-folder> <Proxmox-host>")
    folder, host = sys.argv[1:]
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host) or host.startswith(".") or host.endswith("."):
        raise SetupError("Proxmox host must be an IP address or DNS name without spaces")

    base_url = read_file(folder, "infisical-url").rstrip("/")
    credential_id = read_file(folder, "credential-id")
    project_id = read_file(folder, "project-id")
    environment = read_file(folder, "environment")
    project_slug = read_file(folder, "project-slug", required=False)
    read_id = read_file(folder, "read-client-id")
    read_secret = read_file(folder, "read-client-secret")
    write_id = read_file(folder, "write-client-id")
    write_secret = read_file(folder, "write-client-secret")
    private_key = read_file(folder, "pve-private-key") + "\n"
    known_hosts = read_file(folder, "pve-known-hosts") + "\n"
    public_key = read_file(folder, "pve-public-key")

    if not base_url.startswith("https://") or any(char.isspace() for char in base_url):
        raise SetupError("Infisical URL must be an HTTPS URL")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,99}", credential_id):
        raise SetupError("Jenkins credential ID must be 3-100 letters, numbers, dots, underscores, or hyphens")
    if not re.fullmatch(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", project_id):
        raise SetupError("Infisical project ID must be a UUID")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", environment):
        raise SetupError("Infisical environment must contain only letters, numbers, underscores, or hyphens")
    if "/" in project_slug or "\n" in project_slug or "\r" in project_slug:
        raise SetupError("Infisical project slug must not contain slashes or line breaks")
    if not re.fullmatch(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", read_id):
        raise SetupError("The read-only Infisical Client ID must be a UUID")
    if not re.fullmatch(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", write_id):
        raise SetupError("The read/write Infisical Client ID must be a UUID")
    if len(read_secret) < 16 or len(write_secret) < 16:
        raise SetupError("Both Infisical Client Secrets must contain at least 16 characters")
    if not private_key.startswith("-----BEGIN OPENSSH PRIVATE KEY-----"):
        raise SetupError("Generated Proxmox SSH private key is invalid")
    key_fields = public_key.split()
    if len(key_fields) < 2 or key_fields[0] != "ssh-ed25519":
        raise SetupError("Generated Proxmox SSH public key is invalid")
    known_fields = known_hosts.split()
    if len(known_fields) < 3 or known_fields[0] != host or known_fields[1] != "ssh-ed25519":
        raise SetupError("Local Proxmox host key does not match the configured host")

    reader = login(base_url, read_id, read_secret, "read-only")
    writer = login(base_url, write_id, write_secret, "read/write")
    query = urllib.parse.urlencode({
        "projectId": project_id,
        "environment": environment,
        "secretPath": "/proxmox/automation",
    })

    def secret_url(name):
        return f"{base_url}/api/v4/secrets/{urllib.parse.quote(name)}?{query}"

    def read_secret_value(token, name):
        result = request_json(secret_url(name), token=token, allow_404=True)
        if not result:
            return None
        secret = result.get("secret", {}) if isinstance(result, dict) else {}
        value = secret.get("secretValue") if isinstance(secret, dict) else None
        return value if isinstance(value, str) else None

    def write_secret(name, value):
        body = {
            "projectId": project_id,
            "environment": environment,
            "secretValue": value,
            "secretPath": "/proxmox/automation",
            "type": "shared",
            "skipMultilineEncoding": True,
        }
        method = "PATCH" if read_secret_value(reader, name) is not None else "POST"
        request_json(secret_url(name), method=method, token=writer, body=body)

    names = ("PVE_SSH_PRIVATE_KEY", "PVE_SSH_HOST_KEY")
    previous = {name: read_secret_value(reader, name) for name in names}
    try:
        write_secret(names[0], private_key)
        write_secret(names[1], known_hosts)
        if read_secret_value(reader, names[0]) != private_key or read_secret_value(reader, names[1]) != known_hosts:
            raise SetupError("Infisical read-only identity could not verify the stored Proxmox SSH secrets")
        update_authorized_keys(public_key)
    except Exception:
        rollback_errors = []
        for name, value in previous.items():
            try:
                if value is None:
                    request_json(secret_url(name), method="DELETE", token=writer, allow_404=True)
                else:
                    write_secret(name, value)
            except Exception:
                rollback_errors.append(name)
        if rollback_errors:
            raise SetupError("Infisical setup failed and previous SSH secret values could not all be restored; inspect PVE_SSH_PRIVATE_KEY and PVE_SSH_HOST_KEY") from None
        raise

    print("Both Infisical Machine Identities authenticated.")
    print("Generated Proxmox SSH private key and local host key were stored and verified at /proxmox/automation.")
    print("Installed the matching SSH public key for root on this Proxmox host.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        if isinstance(exc, SetupError):
            print(f"ERROR: {exc}", file=sys.stderr)
        else:
            print("ERROR: Infisical bootstrap failed; response details were suppressed.", file=sys.stderr)
        sys.exit(1)
