#!/usr/bin/env python3
"""Create or safely reuse DNS LXCs on Proxmox, configure Technitium, and sync zones."""

import ipaddress
import json
import os
import re
import shlex
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import paramiko
from paramiko.hostkeys import HostKeyEntry

ROOT = Path(__file__).resolve().parents[3]
PROFILES = {
    "dns01": (150, "02:00:00:00:01:50", "dns01.profile.sh"),
    "dns02": (151, "02:00:00:00:01:51", "dns02.profile.sh"),
}


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required Jenkins setting {name} is missing")
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


def read_infisical():
    base = required("INFISICAL_URL").rstrip("/")
    if not base.startswith("https://"):
        raise RuntimeError("INFISICAL_URL must use HTTPS")
    login = http_json(f"{base}/api/v1/auth/universal-auth/login", "POST", form={
        "clientId": required("INFISICAL_READ_CLIENT_ID"),
        "clientSecret": required("INFISICAL_READ_CLIENT_SECRET"),
    })
    token = login.get("accessToken") if isinstance(login, dict) else None
    if not token:
        raise RuntimeError("Infisical read identity did not authenticate")
    project = required("INFISICAL_PROJECT_ID")
    environment = required("INFISICAL_ENVIRONMENT")
    values = {}

    def fetch(path, names, optional=()):
        query = urllib.parse.urlencode({"projectId": project, "environment": environment, "secretPath": path})
        for name in names:
            try:
                response = http_json(f"{base}/api/v4/secrets/{urllib.parse.quote(name)}?{query}", headers={"Authorization": f"Bearer {token}"})
            except RuntimeError as exc:
                if name in optional and "HTTP 404" in str(exc):
                    values[name] = ""
                    continue
                raise
            secret = response.get("secret", {}) if isinstance(response, dict) else {}
            value = secret.get("secretValue") if isinstance(secret, dict) else None
            if not isinstance(value, str) or not value.strip():
                raise RuntimeError(f"Infisical secret {path}/{name} is missing or empty")
            values[name] = value

    fetch("/proxmox/automation", ("PVE_SSH_PRIVATE_KEY", "PVE_SSH_HOST_KEY"))
    fetch("/dns", (
        "DNS_PUBLIC_FALLBACKS", "MIKROTIK_DHCP_DNS_MODE", "DNS_HOSTED_ZONES",
    ), optional=("DNS_PUBLIC_FALLBACKS", "DNS_HOSTED_ZONES"))
    for role in ("dns01", "dns02"):
        fetch(f"/dns/{role}", ("DNS_IPV4", "DNS_SERVER_ADMIN_PASSWORD"))
        values[f"{role.upper()}_IPV4"] = values.pop("DNS_IPV4")
        values[f"{role.upper()}_ADMIN_PASSWORD"] = values.pop("DNS_SERVER_ADMIN_PASSWORD")
    for role in ("dns01", "dns02"):
        fetch(f"/proxmox/lxc/{role}", ("LXC_ROOT_PASSWORD",))
        values[f"{role.upper()}_ROOT_PASSWORD"] = values.pop("LXC_ROOT_PASSWORD")
    for field in ("DNS01_IPV4", "DNS02_IPV4"):
        values[field] = str(ipaddress.IPv4Address(values[field].strip()))
    if values["DNS01_IPV4"] == values["DNS02_IPV4"]:
        raise RuntimeError("DNS01_IPV4 and DNS02_IPV4 must be different")
    for field in ("DNS01_ADMIN_PASSWORD", "DNS02_ADMIN_PASSWORD"):
        if len(values[field]) < 6 or any(char in values[field] for char in "\r\n\0"):
            raise RuntimeError(f"{field} must be at least 6 characters and contain no line breaks")
    for role in ("dns01", "dns02"):
        root_password = values[f"{role.upper()}_ROOT_PASSWORD"]
        if len(root_password) < 6 or ":" in root_password or any(char in root_password for char in "\r\n\0"):
            raise RuntimeError(f"/proxmox/lxc/{role}/LXC_ROOT_PASSWORD is invalid")
    return values


def ssh_settings(host):
    key_text = values_for_ssh["PVE_SSH_PRIVATE_KEY"]
    lines = [line.strip() for line in values_for_ssh["PVE_SSH_HOST_KEY"].splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if len(lines) != 1:
        raise RuntimeError("Infisical PVE_SSH_HOST_KEY must contain one known_hosts entry")
    fields = lines[0].split()
    if len(fields) < 3 or fields[0] not in (host, f"[{host}]:22"):
        raise RuntimeError("Infisical PVE_SSH_HOST_KEY does not match the configured Proxmox host")
    entry = HostKeyEntry.from_line(" ".join(fields))
    if entry is None:
        raise RuntimeError("Could not parse the pinned Proxmox SSH host key")
    key_type, pinned = entry.key.get_name(), entry.key
    key_file = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="dns-pve-key-", delete=False)
    try:
        key_file.write(key_text.rstrip("\n") + "\n")
        key_file.close()
        os.chmod(key_file.name, 0o600)
        key = paramiko.Ed25519Key.from_private_key_file(key_file.name)
    except Exception:
        key_file.close()
        Path(key_file.name).unlink(missing_ok=True)
        raise RuntimeError("Could not load the pinned Proxmox SSH private key") from None
    client = paramiko.SSHClient()
    client.get_host_keys().add(host, key_type, pinned)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(hostname=host, username="root", pkey=key, look_for_keys=False, allow_agent=False,
                       timeout=20, banner_timeout=20, auth_timeout=20)
    except Exception:
        client.close()
        Path(key_file.name).unlink(missing_ok=True)
        raise RuntimeError("Could not authenticate to Proxmox using the pinned SSH key and host key") from None
    return client, key_file.name


def remote(client, command, timeout=900, capture=True):
    try:
        _stdin, stdout, _stderr = client.exec_command(command + " 2>&1", timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        status = stdout.channel.recv_exit_status()
    except Exception:
        raise RuntimeError("Proxmox SSH operation failed; remote output was suppressed") from None
    if status:
        raise RuntimeError(f"Proxmox operation failed with exit status {status}")
    return out.strip() if capture else ""


def upload(sftp, local_path, remote_path, mode=0o600):
    sftp.put(str(local_path), remote_path)
    sftp.chmod(remote_path, mode)


def matches_container_identity(config, role, mac):
    """Require exact identity markers before an existing guest can be reused."""
    lines = {}
    for line in config.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            lines[key.strip()] = value.strip()

    network = {}
    for item in lines.get("net0", "").split(","):
        if "=" in item:
            key, value = item.strip().split("=", 1)
            network[key] = value

    tags = {tag.strip() for tag in lines.get("tags", "").split(";") if tag.strip()}
    return (
        lines.get("hostname") == role
        and network.get("hwaddr", "").upper() == mac.upper()
        and network.get("bridge") == "vmbr0"
        and network.get("ip") == "dhcp"
        and network.get("tag") == "30"
        and {"community-script", "dns", "homelab", "managed-by-jenkins"}.issubset(tags)
    )


def execute_in_ct(client, ct, remote_script, config, action):
    sftp = client.open_sftp()
    remote_config = f"{remote_script}.{action}.json"
    try:
        with sftp.file(remote_config, "wb") as handle:
            handle.write(json.dumps(config, separators=(",", ":")).encode())
        sftp.chmod(remote_config, 0o600)
    finally:
        sftp.close()
    guest_script = "/run/homelab-dns-control/manage-technitium.py"
    guest_config = f"/run/homelab-dns-control/{action}.json"
    try:
        remote(client, f"pct exec {ct} -- mkdir -p /run/homelab-dns-control", timeout=60)
        remote(client, f"pct push {ct} {shlex.quote(remote_script)} {guest_script} --perms 600", timeout=60)
        remote(client, f"pct push {ct} {shlex.quote(remote_config)} {guest_config} --perms 600", timeout=60)
        return remote(client, f"pct exec {ct} -- python3 {guest_script} {guest_config}", timeout=180)
    finally:
        try:
            remote(client, f"pct exec {ct} -- rm -rf /run/homelab-dns-control", timeout=60)
        except Exception:
            print(f"WARNING: temporary configuration cleanup failed inside CTID {ct}", file=sys.stderr)
        try:
            remote(client, f"rm -f -- {shlex.quote(remote_config)}", timeout=60)
        except Exception:
            pass


def provision(client, stage, values):
    remote(client, "command -v pct >/dev/null && command -v curl >/dev/null && command -v python3 >/dev/null", timeout=30)
    # main() creates this directory with mktemp -d; only tighten its mode here.
    remote(client, f"chmod 700 {shlex.quote(stage)}", timeout=30)
    sftp = client.open_sftp()
    try:
        upload(sftp, ROOT / "proxmox_helper_script/create-lxc.sh", f"{stage}/create-lxc.sh", 0o700)
        upload(sftp, ROOT / "jenkins/pipelines/dns-deploy/manage-technitium.py", f"{stage}/manage-technitium.py", 0o700)
        for role, (ctid, _mac, profile_name) in PROFILES.items():
            upload(sftp, ROOT / "proxmox_helper_script/lxc/technitium_dns" / profile_name, f"{stage}/{role}.profile.sh", 0o600)
            with sftp.file(f"{stage}/{role}.root-password", "wb") as handle:
                handle.write((values[f"{role.upper()}_ROOT_PASSWORD"] + "\n").encode())
            sftp.chmod(f"{stage}/{role}.root-password", 0o600)
    finally:
        sftp.close()

    for role, (ctid, mac, _profile) in PROFILES.items():
        hostname = role
        is_present = remote(client, f"if pct status {ctid} >/dev/null 2>&1; then printf yes; else printf no; fi", timeout=30)
        if is_present == "yes":
            existing = remote(client, f"pct config {ctid}", timeout=30)
            if not matches_container_identity(existing, hostname, mac):
                raise RuntimeError(f"CTID {ctid} already exists but does not match the {role} profile; refusing to modify or replace it")
            status = remote(client, f"pct status {ctid}", timeout=30)
            if "status: running" not in status:
                remote(client, f"pct start {ctid}", timeout=120)
            print(f"Verified and reused {role} (CTID {ctid}).")
            continue
        password_file = f"{stage}/{role}.root-password"
        profile = f"{stage}/{role}.profile.sh"
        command = f"HOMELAB_LXC_ROOT_PASSWORD_FILE={shlex.quote(password_file)} bash {shlex.quote(stage + '/create-lxc.sh')} {shlex.quote(profile)}"
        remote(client, command, timeout=3600)

    # Wait for LXC service startup and verify the expected DHCP reservation is active.
    for role, (ctid, _mac, _profile) in PROFILES.items():
        expected_ip = values[f"{role.upper()}_IPV4"]
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            guest_ip = remote(client, f"pct exec {ctid} -- hostname -I", timeout=30)
            if expected_ip in guest_ip.split():
                break
            time.sleep(10)
        else:
            raise RuntimeError(f"{role} did not receive its configured DHCP reservation {expected_ip}")
        remote(client, f"pct exec {ctid} -- test -x /usr/bin/python3", timeout=30)

    # Install credentials/config only inside guest /run (tmpfs); configure local API over loopback.
    for role, (ctid, _mac, _profile) in PROFILES.items():
        admin_password = values[f"{role.upper()}_ADMIN_PASSWORD"]
        action = "verify"
        config = {"admin_password": admin_password, "action": action}
        execute_in_ct(client, ctid, f"{stage}/manage-technitium.py", config, action)

    hosted_zones = [item.strip().rstrip(".").lower() for item in values["DNS_HOSTED_ZONES"].split(",") if item.strip()]
    if len(hosted_zones) != len(set(hosted_zones)):
        raise RuntimeError("DNS_HOSTED_ZONES contains duplicate names")
    if hosted_zones:
        execute_in_ct(client, 150, f"{stage}/manage-technitium.py", {
            "admin_password": values["DNS01_ADMIN_PASSWORD"], "action": "ensure-primary-zones", "zones": hosted_zones,
        }, "ensure-primary-zones")

    primary_zones_raw = execute_in_ct(
        client, 150, f"{stage}/manage-technitium.py",
        {"admin_password": values["DNS01_ADMIN_PASSWORD"], "action": "list-zones"}, "list-zones",
    )
    try:
        primary_zones = json.loads(primary_zones_raw)
    except json.JSONDecodeError:
        raise RuntimeError("dns01 returned an invalid zone list") from None

    zones_config = {
        "admin_password": values["DNS01_ADMIN_PASSWORD"],
        "action": "allow-transfers",
        "secondary_ipv4": values["DNS02_IPV4"],
        "zones": primary_zones,
    }
    execute_in_ct(client, 150, f"{stage}/manage-technitium.py", zones_config, "allow-transfers")
    secondary_config = {
        "admin_password": values["DNS02_ADMIN_PASSWORD"],
        "action": "configure-secondaries",
        "primary_ipv4": values["DNS01_IPV4"],
        "zones": primary_zones,
    }
    execute_in_ct(client, 151, f"{stage}/manage-technitium.py", secondary_config, "configure-secondaries")
    print(f"Technitium authentication verified; {len(primary_zones)} primary zone(s) synced to dns02.")
    if not primary_zones:
        print("No hosted primary zones exist yet. Future runs create secondary zones for each primary zone found on dns01.")

    # Remove staged credentials and code regardless of successful config.
    remote(client, f"rm -f -- {shlex.quote(stage)}/*.root-password {shlex.quote(stage)}/*.json && rm -rf -- {shlex.quote(stage)}", timeout=60)


values_for_ssh = {}


def main():
    global values_for_ssh
    values = read_infisical()
    values_for_ssh = values
    host = required("PROXMOX_HOST")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        raise RuntimeError("PROXMOX_HOST must be a hostname or IPv4 address")
    client, key_file = ssh_settings(host)
    stage = ""
    try:
        candidate_stage = remote(client, "umask 077; mktemp -d /tmp/homelab-dns-deploy.XXXXXX", timeout=30)
        if not re.fullmatch(r"/tmp/homelab-dns-deploy\.[A-Za-z0-9]{6}", candidate_stage):
            raise RuntimeError("Proxmox returned an unexpected temporary directory")
        stage = candidate_stage
        provision(client, stage, values)
    finally:
        if stage:
            try:
                remote(client, f"rm -rf -- {shlex.quote(stage)}", timeout=30)
            except Exception:
                print("WARNING: temporary Proxmox DNS staging folder could not be removed", file=sys.stderr)
        client.close()
        Path(key_file).unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
