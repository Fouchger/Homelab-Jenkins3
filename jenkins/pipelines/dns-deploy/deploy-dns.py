#!/usr/bin/env python3
"""Create or safely reuse DNS LXCs on Proxmox, configure Technitium, and sync zones."""

import ipaddress
import hashlib
import json
import os
import re
import shlex
import sys
import subprocess
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


def read_infisical(*, require_backup_password=False):
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
        "DNS_PUBLIC_FALLBACKS", "MIKROTIK_DHCP_DNS_MODE", "DNS_HOSTED_ZONES", "DNS_BACKUP_ENCRYPTION_PASSWORD",
    ), optional=("DNS_PUBLIC_FALLBACKS", "DNS_HOSTED_ZONES", "DNS_BACKUP_ENCRYPTION_PASSWORD"))
    if require_backup_password:
        password = values.get("DNS_BACKUP_ENCRYPTION_PASSWORD", "")
        if len(password) < 32 or any(char in password for char in "\r\n\0"):
            raise RuntimeError("Selected DNS recreation requires /dns/DNS_BACKUP_ENCRYPTION_PASSWORD with at least 32 characters")
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


def ssh_settings(host, values):
    key_text = values["PVE_SSH_PRIVATE_KEY"]
    lines = [line.strip() for line in values["PVE_SSH_HOST_KEY"].splitlines() if line.strip() and not line.lstrip().startswith("#")]
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


def remote(client, command, timeout=900):
    try:
        _stdin, stdout, _stderr = client.exec_command(command + " 2>&1", timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        status = stdout.channel.recv_exit_status()
    except Exception:
        raise RuntimeError("Proxmox SSH operation failed; remote output was suppressed") from None
    if status:
        raise RuntimeError(f"Proxmox operation failed with exit status {status}")
    return out.strip()


def upload(sftp, local_path, remote_path, mode=0o600):
    sftp.put(str(local_path), remote_path)
    sftp.chmod(remote_path, mode)


def backup_container(client, stage, role, ctid, password):
    """Create, verify, encrypt, and retain a Proxmox LXC backup."""
    backup_dir = f"{stage}/backups"
    remote(client, f"install -d -m 700 {shlex.quote(backup_dir)}", timeout=30)
    output = remote(client, f"vzdump {ctid} --mode stop --compress zstd --dumpdir {shlex.quote(backup_dir)}", timeout=3600)
    matches = re.findall(r"(?:creating vzdump archive|archive):? ['\"]?([^'\"\s]+\.tar\.(?:zst|gz|lzo))", output)
    if len(matches) != 1:
        raise RuntimeError(f"Proxmox did not report one unambiguous backup archive for {role}; refusing to delete CTID {ctid}")
    archive = matches[0]
    remote(client, f"test -s {shlex.quote(archive)}", timeout=30)
    build = os.environ.get("BUILD_NUMBER", "local")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", build):
        raise RuntimeError("BUILD_NUMBER is invalid for backup artifact storage")
    destination_dir = ROOT / "artifacts" / build / "dns-container-backups"
    destination_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = destination_dir / (Path(archive).name + ".gpg")
    destination.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{role}-", dir=destination_dir) as temp_dir:
        os.chmod(temp_dir, 0o700)
        plaintext = Path(temp_dir) / Path(archive).name
        password_file = Path(temp_dir) / "passphrase"
        sftp = client.open_sftp()
        try:
            sftp.get(archive, str(plaintext))
        finally:
            sftp.close()
        os.chmod(plaintext, 0o600)
        digest = hashlib.sha256()
        with plaintext.open("rb") as backup_file:
            for chunk in iter(lambda: backup_file.read(1024 * 1024), b""):
                digest.update(chunk)
        local_hash = digest.hexdigest()
        hash_fields = remote(client, f"sha256sum {shlex.quote(archive)}", timeout=60).split()
        remote_hash = hash_fields[0] if hash_fields else ""
        if not re.fullmatch(r"[0-9a-f]{64}", remote_hash) or local_hash != remote_hash:
            raise RuntimeError(f"Downloaded backup for {role} failed SHA-256 verification; refusing to delete CTID {ctid}")
        password_file.write_text(password + "\n", encoding="utf-8")
        os.chmod(password_file, 0o600)
        try:
            encrypted = subprocess.run(
                ["gpg", "--batch", "--yes", "--pinentry-mode", "loopback", "--symmetric",
                 "--cipher-algo", "AES256", "--s2k-mode", "3", "--s2k-digest-algo", "SHA512",
                 "--s2k-count", "65011712", "--passphrase-file", str(password_file),
                 "--output", str(destination), str(plaintext)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=3600, check=False,
            )
            if encrypted.returncode != 0 or not destination.is_file() or destination.stat().st_size == 0:
                raise RuntimeError(f"Could not encrypt the pre-recreation backup for {role}; refusing to delete CTID {ctid}")
            verified = subprocess.run(
                ["gpg", "--batch", "--yes", "--pinentry-mode", "loopback", "--passphrase-file",
                 str(password_file), "--decrypt", "--output", os.devnull, str(destination)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=3600, check=False,
            )
            if verified.returncode != 0:
                raise RuntimeError(f"Encrypted backup verification failed for {role}; refusing to delete CTID {ctid}")
        except (OSError, subprocess.SubprocessError, RuntimeError):
            destination.unlink(missing_ok=True)
            raise
        os.chmod(destination, 0o600)
    print(f"Verified encrypted pre-recreation backup for {role}: {destination.relative_to(ROOT)}")
    return destination


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


def selected_recreate_roles():
    actions = {
        role: os.environ.get(f"HOMELAB_{role.upper()}_ACTION", "reuse").strip().lower() or "reuse"
        for role in PROFILES
    }
    invalid = [f"{role}={action}" for role, action in actions.items()
               if action not in ("reuse", "destroy-and-recreate")]
    if invalid:
        raise RuntimeError(
            "DNS server actions must be reuse or destroy-and-recreate: " + ", ".join(invalid)
        )
    selected = [role for role, action in actions.items() if action == "destroy-and-recreate"]
    return selected


def recreation_order(client, recreate_roles):
    """Prefer creating an absent DNS peer before destroying the surviving server."""
    order = list(recreate_roles)
    if set(order) != {"dns01", "dns02"}:
        return order
    presence = {}
    for role, (ctid, _mac, _profile) in PROFILES.items():
        result = remote(client, f"if pct status {ctid} >/dev/null 2>&1; then printf yes; else printf no; fi", timeout=30)
        presence[role] = result == "yes"
    if presence["dns01"] and not presence["dns02"]:
        return ["dns02", "dns01"]
    return order


def ensure_peer_dns_online(client, stage, role, values):
    """Verify the non-selected server is healthy before taking its peer offline."""
    ctid, mac, _profile = PROFILES[role]
    present = remote(client, f"if pct status {ctid} >/dev/null 2>&1; then printf yes; else printf no; fi", timeout=30)
    if present != "yes":
        raise RuntimeError(
            f"Cannot safely recreate the other DNS server: {role} CTID {ctid} is absent, "
            "so there would be no verified DNS server to keep online."
        )
    config = remote(client, f"pct config {ctid}", timeout=30)
    if not matches_container_identity(config, role, mac):
        raise RuntimeError(f"Cannot safely recreate the other DNS server: CTID {ctid} does not match {role} identity")
    status = remote(client, f"pct status {ctid}", timeout=30)
    if "status: running" not in status:
        print(f"Starting {role} (CTID {ctid}) so it can remain online during peer recreation.")
        remote(client, f"pct start {ctid}", timeout=120)

    expected_ip = values[f"{role.upper()}_IPV4"]
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        guest_ips = remote(client, f"pct exec {ctid} -- hostname -I", timeout=30).split()
        if expected_ip in guest_ips:
            break
        time.sleep(10)
    else:
        raise RuntimeError(f"Cannot safely recreate the peer: {role} did not receive expected address {expected_ip}")

    config_path = {
        "admin_password": values[f"{role.upper()}_ADMIN_PASSWORD"],
        "action": "verify",
    }
    result = execute_in_ct(client, ctid, f"{stage}/manage-technitium.py", config_path, "peer-health-check")
    if "DNS listener verified" not in result:
        raise RuntimeError(f"Cannot safely recreate the peer: {role} DNS health check did not confirm service")
    print(f"Safety check passed: {role} (CTID {ctid}, {expected_ip}) is running and answering DNS queries.")


def wait_for_dns_guest_ready(client, stage, role, values):
    ctid, _mac, _profile = PROFILES[role]
    expected_ip = values[f"{role.upper()}_IPV4"]
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        guest_ips = remote(client, f"pct exec {ctid} -- hostname -I", timeout=30).split()
        if expected_ip in guest_ips:
            break
        time.sleep(10)
    else:
        raise RuntimeError(f"{role} did not receive its configured DHCP reservation {expected_ip}")
    remote(client, f"pct exec {ctid} -- test -x /usr/bin/python3", timeout=30)
    config = {"admin_password": values[f"{role.upper()}_ADMIN_PASSWORD"], "action": "verify"}
    result = execute_in_ct(client, ctid, f"{stage}/manage-technitium.py", config, "verify")
    if "DNS listener verified" not in result:
        raise RuntimeError(f"{role} did not pass the Technitium DNS readiness check")
    print(f"Verified {role} is online at {expected_ip} and answering DNS queries.")


def export_zone_files(client, stage, role, values, zone_types):
    ctid = PROFILES[role][0]
    config = {
        "admin_password": values[f"{role.upper()}_ADMIN_PASSWORD"],
        "action": "export-zones",
        "zone_types": zone_types,
    }
    raw = execute_in_ct(client, ctid, f"{stage}/manage-technitium.py", config, "export-zones")
    try:
        zone_files = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"{role} returned invalid exported zone data") from None
    if not isinstance(zone_files, dict):
        raise RuntimeError(f"{role} returned invalid exported zone data")
    print(f"Captured {len(zone_files)} {role} zone file(s) for safe primary-zone restoration.")
    return zone_files


def configure_zone_replication(client, stage, values, preserved_zone_files=None):
    hosted_zones = [item.strip().rstrip(".").lower() for item in values["DNS_HOSTED_ZONES"].split(",") if item.strip()]
    if len(hosted_zones) != len(set(hosted_zones)):
        raise RuntimeError("DNS_HOSTED_ZONES contains duplicate names")
    if hosted_zones:
        execute_in_ct(client, 150, f"{stage}/manage-technitium.py", {
            "admin_password": values["DNS01_ADMIN_PASSWORD"], "action": "ensure-primary-zones", "zones": hosted_zones,
        }, "ensure-primary-zones")
    if preserved_zone_files:
        execute_in_ct(client, 150, f"{stage}/manage-technitium.py", {
            "admin_password": values["DNS01_ADMIN_PASSWORD"],
            "action": "import-zones",
            "zone_files": preserved_zone_files,
        }, "import-zones")

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


def provision(client, stage, values, recreate_roles):
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

    preserved_dns01_zones = None
    ordered_roles = recreation_order(client, recreate_roles)
    role_items = list(PROFILES.items())
    if ordered_roles == ["dns02", "dns01"]:
        role_items.reverse()
        print("dns02 is absent, so Jenkins will create it first before rebuilding the currently online dns01.")
    for role, (ctid, mac, _profile) in role_items:
        recreate_role = role in recreate_roles
        hostname = role
        is_present = remote(client, f"if pct status {ctid} >/dev/null 2>&1; then printf yes; else printf no; fi", timeout=30)
        if is_present == "yes":
            existing = remote(client, f"pct config {ctid}", timeout=30)
            if not matches_container_identity(existing, hostname, mac):
                raise RuntimeError(f"CTID {ctid} already exists but does not match the {role} profile; refusing to modify or replace it")
            status = remote(client, f"pct status {ctid}", timeout=30)
            if recreate_role:
                print(f"RECREATING {role} (CTID {ctid}): creating and downloading a verified Proxmox backup before deletion.")
                try:
                    peer_role = "dns02" if role == "dns01" else "dns01"
                    print(f"Verifying {peer_role} is healthy before taking {role} offline.")
                    ensure_peer_dns_online(client, stage, peer_role, values)
                    if role == "dns01":
                        preserved_dns01_zones = export_zone_files(client, stage, "dns01", values, ["Primary"])
                    backup_container(client, stage, role, ctid, values["DNS_BACKUP_ENCRYPTION_PASSWORD"])
                    if "status: running" in status:
                        try:
                            remote(client, f"pct shutdown {ctid} --timeout 120", timeout=180)
                        except RuntimeError:
                            pass
                        status = remote(client, f"pct status {ctid}", timeout=30)
                        if "status: stopped" not in status:
                            remote(client, f"pct stop {ctid}", timeout=120)
                    remote(client, f"pct destroy {ctid} --purge 1", timeout=600)
                    still_present = remote(
                        client,
                        f"if pct status {ctid} >/dev/null 2>&1; then printf present; else printf absent; fi",
                        timeout=30,
                    )
                    if still_present != "absent":
                        raise RuntimeError(f"Proxmox still reports CTID {ctid} after destroy")
                except Exception as exc:
                    raise RuntimeError(f"Could not safely remove {role} CTID {ctid}; check its Proxmox status before retrying. {exc}") from None
            else:
                if "status: running" not in status:
                    remote(client, f"pct start {ctid}", timeout=120)
                print(f"Verified and reused {role} (CTID {ctid}).")
                continue
        elif recreate_role:
            print(f"{role} (CTID {ctid}) is absent; creating it cleanly. No existing data will be deleted.")
            peer_role = "dns02" if role == "dns01" else "dns01"
            print(f"Verifying {peer_role} is healthy before creating/replacing {role}.")
            ensure_peer_dns_online(client, stage, peer_role, values)
            if role == "dns01":
                preserved_dns01_zones = export_zone_files(client, stage, peer_role, values, ["Secondary"])
        elif role == "dns01":
            peer_present = remote(client, "if pct status 151 >/dev/null 2>&1; then printf yes; else printf no; fi", timeout=30)
            if peer_present == "yes":
                print("dns01 is absent; verifying dns02 and preserving its secondary zones before creating dns01.")
                ensure_peer_dns_online(client, stage, "dns02", values)
                preserved_dns01_zones = export_zone_files(client, stage, "dns02", values, ["Secondary"])
            else:
                print("dns01 and dns02 are both absent; creating dns01 first, then dns02. No active DNS server is available to preserve.")

        password_file = f"{stage}/{role}.root-password"
        profile = f"{stage}/{role}.profile.sh"
        command = f"HOMELAB_LXC_ROOT_PASSWORD_FILE={shlex.quote(password_file)} bash {shlex.quote(stage + '/create-lxc.sh')} {shlex.quote(profile)}"
        try:
            remote(client, command, timeout=3600)
        except Exception as exc:
            if recreate_role:
                raise RuntimeError(f"{role} recreation was selected, but its clean replacement could not be created after removal; check CTID {ctid} on Proxmox. {exc}") from None
            raise

        wait_for_dns_guest_ready(client, stage, role, values)
        if role == "dns01" and "dns02" in recreate_roles:
            print("dns01 is healthy; synchronizing zones before dns02 is recreated.")
            configure_zone_replication(client, stage, values, preserved_dns01_zones)

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

    configure_zone_replication(client, stage, values, preserved_dns01_zones)

    # Remove staged credentials and code regardless of successful config.
    remote(client, f"rm -f -- {shlex.quote(stage)}/*.root-password {shlex.quote(stage)}/*.json && rm -rf -- {shlex.quote(stage)}", timeout=60)


def review_plan(client, values, recreate_roles):
    """Show the DNS deployment operations without changing Proxmox or DNS."""
    remote(client, "command -v pct >/dev/null && command -v curl >/dev/null && command -v python3 >/dev/null", timeout=30)
    ordered_roles = recreation_order(client, recreate_roles)
    print("Read-only DNS deployment plan (no containers or DNS settings changed):")
    for role, (ctid, mac, _profile) in PROFILES.items():
        expected_ip = values[f"{role.upper()}_IPV4"]
        present = remote(client, f"if pct status {ctid} >/dev/null 2>&1; then printf yes; else printf no; fi", timeout=30)
        if present == "yes":
            config = remote(client, f"pct config {ctid}", timeout=30)
            if not matches_container_identity(config, role, mac):
                raise RuntimeError(f"CTID {ctid} exists but does not match the {role} identity; refusing to approve deployment")
            state = remote(client, f"pct status {ctid}", timeout=30).replace("status: ", "").strip()
            if role in recreate_roles:
                other_role = "dns02" if role == "dns01" else "dns01"
                print(f"- {role}: DESTRUCTIVE — create and verify a Proxmox backup, then delete verified CTID {ctid} ({state}) and create a clean replacement at {expected_ip}; {other_role} stays online during this recreation")
            else:
                print(f"- {role}: reuse verified CTID {ctid} ({state}); verify its address is {expected_ip}")
        else:
            suffix = " (selected for clean recreation; no existing container or data to delete)" if role in recreate_roles else ""
            print(f"- {role}: create CTID {ctid} with reserved address {expected_ip}{suffix}")

    if recreate_roles:
        print(f"- Sequential recreation order: {', '.join(ordered_roles)}; the peer must pass DNS health checks before each destructive step")
        if "dns01" in recreate_roles and "dns02" in recreate_roles:
            print("- dns01 primary zones will be synchronized to dns02 before dns02 is taken offline")

    hosted_zones = [item.strip().rstrip(".").lower() for item in values["DNS_HOSTED_ZONES"].split(",") if item.strip()]
    if len(hosted_zones) != len(set(hosted_zones)):
        raise RuntimeError("DNS_HOSTED_ZONES contains duplicate names")
    print(f"- Technitium dns01 ({values['DNS01_IPV4']}): verify admin access and ensure configured primary zones")
    print(f"- Technitium dns02 ({values['DNS02_IPV4']}): verify admin access and configure secondary copies of dns01 primary zones")
    print(f"- Configured hosted zones to ensure on dns01: {', '.join(hosted_zones) if hosted_zones else '(none; existing primary zones will be synced)'}")
    print("- Router DNS and DHCP settings: reviewed separately below; the apply stage creates an encrypted backup before changes.")


def main():
    arguments = sys.argv[1:]
    if arguments not in ([], ["--plan"]):
        raise RuntimeError("Usage: deploy-dns.py [--plan]")
    recreate_roles = selected_recreate_roles()
    values = read_infisical(require_backup_password=bool(recreate_roles))
    host = required("PROXMOX_HOST")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        raise RuntimeError("PROXMOX_HOST must be a hostname or IPv4 address")
    client, key_file = ssh_settings(host, values)
    stage = ""
    try:
        if arguments == ["--plan"]:
            review_plan(client, values, recreate_roles)
            return
        candidate_stage = remote(client, "umask 077; mktemp -d /tmp/homelab-dns-deploy.XXXXXX", timeout=30)
        if not re.fullmatch(r"/tmp/homelab-dns-deploy\.[A-Za-z0-9]{6}", candidate_stage):
            raise RuntimeError("Proxmox returned an unexpected temporary directory")
        stage = candidate_stage
        provision(client, stage, values, recreate_roles)
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
