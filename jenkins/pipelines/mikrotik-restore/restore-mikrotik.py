#!/usr/bin/env python3
"""Back up, reset, and restore RouterOS using the protected Infisical script."""

import ipaddress
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import paramiko
from paramiko.hostkeys import HostKeyEntry


def required(values, name):
    value = values.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required Infisical value {name} is missing")
    return value


def api_json(url, method="GET", token=None, form=None, allow_404=False):
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
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if allow_404 and exc.code == 404:
            return {}
        raise RuntimeError(f"Infisical request failed with HTTP {exc.code}; response was suppressed") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Infisical request failed: {type(exc).__name__}") from None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("Infisical returned an invalid JSON response") from None


def load_settings():
    base_url = os.environ.get("INFISICAL_URL", "").strip().rstrip("/")
    if not base_url.startswith("https://"):
        raise RuntimeError("INFISICAL_URL must use HTTPS")
    login = api_json(
        f"{base_url}/api/v1/auth/universal-auth/login",
        method="POST",
        form={
            "clientId": os.environ.get("INFISICAL_READ_CLIENT_ID", ""),
            "clientSecret": os.environ.get("INFISICAL_READ_CLIENT_SECRET", ""),
        },
    )
    token = login.get("accessToken") if isinstance(login, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError("Infisical read identity did not authenticate")

    project_id = os.environ.get("INFISICAL_PROJECT_ID", "").strip()
    environment = os.environ.get("INFISICAL_ENVIRONMENT", "").strip()
    if not project_id or not environment:
        raise RuntimeError("Infisical project ID and environment credentials are required")
    values = {}
    for path, names in {
        "/mikrotik/router01": (
            "MIKROTIK_HOST", "MIKROTIK_IP", "MIKROTIK_USERNAME", "MIKROTIK_PASSWORD",
            "MIKROTIK_BOOTSTRAP_USERNAME", "MIKROTIK_BOOTSTRAP_PASSWORD", "MIKROTIK_SSH_USER",
            "MIKROTIK_SSH_PRIVATE_KEY", "MIKROTIK_SSH_PUBLIC_KEY", "MIKROTIK_SSH_HOST_KEY",
        ),
        "/mikrotik/backup": ("BINARY_BACKUP_PASSWORD",),
    }.items():
        query = urllib.parse.urlencode({"projectId": project_id, "environment": environment, "secretPath": path})
        for name in names:
            url = f"{base_url}/api/v4/secrets/{urllib.parse.quote(name)}?{query}"
            result = api_json(url, token=token, allow_404=True)
            secret = result.get("secret", {}) if isinstance(result, dict) else {}
            value = secret.get("secretValue") if isinstance(secret, dict) else None
            values[name] = value if isinstance(value, str) else ""
    for name in ("MIKROTIK_SCRIPT",):
        query = urllib.parse.urlencode({"projectId": project_id, "environment": environment, "secretPath": "/mikrotik/router01"})
        result = api_json(f"{base_url}/api/v4/secrets/{urllib.parse.quote(name)}?{query}", token=token, allow_404=True)
        secret = result.get("secret", {}) if isinstance(result, dict) else {}
        value = secret.get("secretValue") if isinstance(secret, dict) else None
        values[name] = value if isinstance(value, str) else ""
    return values


def ros_quote(value):
    if any(char in value for char in "\r\n\0"):
        raise RuntimeError("RouterOS setting contains an unsupported line break")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$") + '"'


def verify_config_script(script, address):
    if not script.strip() or len(script.encode("utf-8")) > 512 * 1024:
        raise RuntimeError("MIKROTIK_SCRIPT must contain a non-empty RouterOS script no larger than 512 KiB")
    if "\0" in script:
        raise RuntimeError("MIKROTIK_SCRIPT contains an invalid NUL character")
    active_lines = [line.strip() for line in script.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    active_text = "\n".join(active_lines)
    if address not in active_text:
        raise RuntimeError("MIKROTIK_SCRIPT must configure MIKROTIK_IP so Jenkins can reconnect after reset")
    if not re.search(r"(?i)\bethernet2\b|\bether2\b", active_text):
        raise RuntimeError("MIKROTIK_SCRIPT must configure or reference the Proxmox-connected ether2 interface")
    if re.search(r"(?im)^\s*/system\s+reset-configuration\b", active_text):
        raise RuntimeError("MIKROTIK_SCRIPT must not contain another reset-configuration command")

    section = ""
    sanitized = []
    skipped_user_section = False
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("/"):
            words = stripped.split(None, 2)
            section = " ".join(words[:2]).lower() if len(words) > 1 else stripped.lower()
            skipped_user_section = section.startswith("/user")
            if not skipped_user_section:
                sanitized.append(line)
        elif not skipped_user_section:
            sanitized.append(line)
    script = "\n".join(sanitized).strip() + "\n"
    sanitized_active = "\n".join(line.strip() for line in script.splitlines() if line.strip() and not line.lstrip().startswith("#"))
    if address not in sanitized_active:
        raise RuntimeError("MIKROTIK_SCRIPT must set MIKROTIK_IP outside its /user configuration sections so Jenkins can reconnect")
    if not re.search(r"(?i)\bethernet2\b|\bether2\b", sanitized_active):
        raise RuntimeError("MIKROTIK_SCRIPT must configure or reference the Proxmox-connected ether2 interface outside its /user sections")
    return script


def known_host_key(value, host):
    lines = [line.strip() for line in value.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if len(lines) != 1:
        raise RuntimeError("MIKROTIK_SSH_HOST_KEY must contain one independently verified known_hosts entry")
    fields = lines[0].split()
    if len(fields) < 3 or fields[0] != host or not fields[1].startswith("ssh-"):
        raise RuntimeError("MIKROTIK_SSH_HOST_KEY does not match MIKROTIK_HOST")
    entry = HostKeyEntry.from_line(" ".join(fields))
    if entry is None:
        raise RuntimeError("Could not parse the pinned MikroTik SSH host key")
    return fields[1], entry.key


def connect(host, user, host_key, *, password=None, key_filename=None, timeout=15):
    key_type, pinned_key = host_key
    client = paramiko.SSHClient()
    client.get_host_keys().add(host, key_type, pinned_key)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    options = {
        "hostname": host,
        "username": user,
        "look_for_keys": False,
        "allow_agent": False,
        "timeout": timeout,
        "banner_timeout": timeout,
        "auth_timeout": timeout,
    }
    if password is not None:
        options["password"] = password
    else:
        options["key_filename"] = key_filename
    try:
        client.connect(**options)
    except Exception:
        client.close()
        raise RuntimeError("Could not authenticate to the MikroTik using the pinned SSH host key") from None
    return client


def command(client, value, *, allow_disconnect=False):
    try:
        _stdin, stdout, stderr = client.exec_command(value, timeout=45)
        output = stdout.read().decode("utf-8", errors="replace")
        error = stderr.read().decode("utf-8", errors="replace")
        status = stdout.channel.recv_exit_status()
    except Exception:
        if allow_disconnect:
            return ""
        raise RuntimeError("RouterOS command failed; command output was suppressed") from None
    if not allow_disconnect and (status != 0 or re.search(r"(?im)^\s*(failure|error|script error):|found [1-9][0-9]* error", output + "\n" + error)):
        raise RuntimeError("RouterOS rejected a command; command output was suppressed")
    return output


def save_backup(client, password):
    build_number = os.environ.get("BUILD_NUMBER", "").strip()
    if not re.fullmatch(r"[0-9]+", build_number):
        raise RuntimeError("Jenkins BUILD_NUMBER is missing or invalid")
    artifact_dir = Path("artifacts") / build_number
    artifact_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(artifact_dir, 0o700)
    name = f"homelab-router-before-reset-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.backup"
    command(client, f"/system/backup/save name={name} password={ros_quote(password)}")
    try:
        sftp = client.open_sftp()
        try:
            try:
                sftp.get("/" + name, str(artifact_dir / name))
            except OSError:
                sftp.get(name, str(artifact_dir / name))
        finally:
            sftp.close()
    except Exception:
        raise RuntimeError("Encrypted router backup was created but could not be downloaded; reset was not started") from None
    local_backup = artifact_dir / name
    if not local_backup.is_file() or local_backup.stat().st_size == 0:
        raise RuntimeError("Encrypted router backup download was empty; reset was not started")
    os.chmod(local_backup, 0o600)
    command(client, f"/file/remove [find where name={ros_quote(name)}]", allow_disconnect=False)
    print(f"Downloaded encrypted pre-reset backup as Jenkins artifact {name}.")


def upload_text(client, remote_name, content):
    sftp = client.open_sftp()
    try:
        with sftp.file(remote_name, "wb") as target:
            target.write(content.encode("utf-8"))
            target.flush()
        if sftp.stat(remote_name).st_size != len(content.encode("utf-8")):
            raise RuntimeError("Uploaded RouterOS script size did not match the Infisical source")
    finally:
        sftp.close()


def build_wrapper(config_path, wrapper_path, marker, admin_user, admin_password, ssh_user, ssh_public_key):
    lines = [":delay 10s"]
    user = ros_quote(admin_user)
    password = ros_quote(admin_password)
    lines.append(f":if ([:len [/user find where name={user}]] = 0) do={{ /user add name={user} group=full password={password} }} else={{ /user set [find where name={user}] group=full password={password} }}")
    if ssh_user != admin_user:
        ssh_name = ros_quote(ssh_user)
        lines.append(f":if ([:len [/user find where name={ssh_name}]] = 0) do={{ /user add name={ssh_name} group=full }} else={{ /user set [find where name={ssh_name}] group=full }}")
    lines.extend((
        f"/user ssh-keys add user={ros_quote(ssh_user)} key={ros_quote(ssh_public_key)}",
        f"/import file-name={config_path} verbose=yes",
        *(['/user disable [find where name="admin"]'] if admin_user != "admin" else []),
        f':log info "{marker}"',
        f"/file/remove [find where name={ros_quote(config_path)}]",
        f"/file/remove [find where name={ros_quote(wrapper_path)}]",
    ))
    return "\n".join(lines) + "\n"


def verify_and_reconnect(address, ssh_user, key_file, host_key, marker, deadline):
    pin = (host_key[0], host_key[1])
    last_error = "Router has not returned yet"
    while time.monotonic() < deadline:
        client = None
        try:
            client = connect(address, ssh_user, pin, key_filename=key_file, timeout=8)
            command(client, "/system/resource/get version")
            command(client, "/ip/address/print")
            marker_count = command(client, f"/log/print count-only where message={ros_quote(marker)}").strip()
            if marker_count != "1":
                raise RuntimeError("Router is reachable, but the complete configuration did not report successful import")
            client.close()
            print(f"Router restored and verified at {address}; Jenkins key authentication is working.")
            return
        except RuntimeError as exc:
            last_error = str(exc)
            if client:
                client.close()
            time.sleep(15)
    raise RuntimeError(f"Reset was initiated but the router did not return with verified key access at MIKROTIK_IP={address}. Check the Proxmox-connected ether2 link, script log on the router, and pinned host key. Last safe status: {last_error}")


def main():
    arguments = sys.argv[1:]
    if arguments not in ([], ["--check"]):
        raise RuntimeError("Usage: restore-mikrotik.py [--check]")
    check_only = arguments == ["--check"]
    values = load_settings()
    current_host = required(values, "MIKROTIK_HOST")
    address = str(ipaddress.IPv4Address(required(values, "MIKROTIK_IP")))
    bootstrap_user = required(values, "MIKROTIK_BOOTSTRAP_USERNAME")
    admin_user = required(values, "MIKROTIK_USERNAME")
    ssh_user = required(values, "MIKROTIK_SSH_USER")
    if not all(re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", user) for user in (bootstrap_user, admin_user, ssh_user)):
        raise RuntimeError("MikroTik usernames must contain only letters, digits, underscore, period, or hyphen")
    bootstrap_password = required(values, "MIKROTIK_BOOTSTRAP_PASSWORD")
    admin_password = required(values, "MIKROTIK_PASSWORD")
    backup_password = required(values, "BINARY_BACKUP_PASSWORD")
    if any(len(value) < 8 for value in (bootstrap_password, admin_password, backup_password)):
        raise RuntimeError("MikroTik bootstrap, admin, and backup passwords must each be at least 8 characters")
    private_key = required(values, "MIKROTIK_SSH_PRIVATE_KEY")
    public_key = required(values, "MIKROTIK_SSH_PUBLIC_KEY")
    script = verify_config_script(required(values, "MIKROTIK_SCRIPT"), address)
    if not private_key.startswith("-----BEGIN OPENSSH PRIVATE KEY-----") or not public_key.startswith("ssh-ed25519 "):
        raise RuntimeError("Run Job 003 first to generate the MikroTik Ed25519 SSH client key pair")
    host_key = known_host_key(required(values, "MIKROTIK_SSH_HOST_KEY"), current_host)
    if any(char in value for value in (backup_password, admin_password, bootstrap_password) for char in "\r\n\0"):
        raise RuntimeError("MikroTik passwords must not contain line breaks or NUL characters")

    key_path = Path("mikrotik-restore-key")
    key_path.write_text(private_key.rstrip("\n") + "\n", encoding="utf-8")
    os.chmod(key_path, 0o600)
    client = None
    try:
        client = connect(current_host, bootstrap_user, host_key, password=bootstrap_password)
        if check_only:
            resource = command(client, "/system/resource print")
            version = re.search(r"(?im)^version:\s*(\S+)", resource)
            print("Readiness check passed: pinned SSH connection and reset/restore settings are valid.")
            print(f"RouterOS: {version.group(1) if version else 'version unavailable'}")
            print(f"Recovery address: {address} over the configured Proxmox-connected ether2 path.")
            print("No backup or router changes were made. The encrypted backup and RouterOS dry-run happen after approval.")
            return
        save_backup(client, backup_password)

        build_number = os.environ.get("BUILD_NUMBER", "")
        config_name = f"homelab-config-{build_number}.rsc"
        wrapper_name = f"homelab-reset-{build_number}.rsc"
        marker = f"homelab-restore-success-{build_number}"
        # On RouterOS devices exposing a flash directory, place the one-shot
        # files there so they survive reset/reboot.
        sftp = client.open_sftp()
        try:
            entries = sftp.listdir(".")
            remote_dir = "flash/" if "flash" in entries else ""
        finally:
            sftp.close()
        config_path = remote_dir + config_name
        wrapper_path = remote_dir + wrapper_name
        wrapper = build_wrapper(config_path, wrapper_path, marker, admin_user, admin_password, ssh_user, public_key)
        upload_text(client, config_path, script)
        upload_text(client, wrapper_path, wrapper)
        command(client, f"/import file-name={config_path} verbose=yes dry-run")
        print("RouterOS dry-run accepted the complete configuration script.")

        # Save a one-shot RouterOS script which performs the reset from script
        # context (avoids an interactive confirmation prompt on SSH exec).
        reset_script = f"/system/reset-configuration no-defaults=yes skip-backup=yes run-after-reset={wrapper_path}"
        trigger_name = f"homelab-reset-trigger-{build_number}"
        command(client, f"/system/script/add name={trigger_name} source={ros_quote(reset_script)}")
        print(f"Starting the approved full reset. Jenkins will reconnect to {address} over the configured ether2 path.")
        command(client, f"/system/script/run [find where name={trigger_name}]", allow_disconnect=True)
        client.close()
        client = None
        deadline = time.monotonic() + 15 * 60
        verify_and_reconnect(address, ssh_user, str(key_path), host_key, marker, deadline)
        # Apply Infisical-managed DNS/DHCP mode and Wi-Fi passphrases over the
        # restored key-authenticated connection, with its own encrypted backup.
        followup_env = os.environ.copy()
        # The reset-only ether2 address can differ from the usual management
        # endpoint. Reuse the same pinned router key, but connect to recovery IP.
        followup_env["HOMELAB_MIKROTIK_CONNECT_HOST"] = address
        followup = subprocess.run(
            [sys.executable, "jenkins/pipelines/mikrotik-config/apply-mikrotik-config.py"],
            check=False,
            timeout=10 * 60,
            env=followup_env,
        )
        if followup.returncode:
            raise RuntimeError("Router reset and full-script import succeeded, but the Infisical DNS/Wi-Fi follow-up failed; encrypted backups are available in this Jenkins build")
    finally:
        if client:
            client.close()
        try:
            key_path.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
