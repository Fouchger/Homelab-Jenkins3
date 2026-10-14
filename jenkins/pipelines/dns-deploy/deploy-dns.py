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
JENKINS_AGENT_CTID = 101
JENKINS_AGENT_MAC = "BC:24:11:5E:D9:F9"
JENKINS_AGENT_IPV4 = "192.168.20.6"
JENKINS_AGENT_NAMESERVERS = ("192.168.30.2", "192.168.30.3")
JENKINS_AGENT_SEARCHDOMAIN = "fouchger.uk"
PROXMOX_API_HOSTNAME = "pve01.fouchger.uk"
PROXMOX_API_IPV4 = "192.168.20.10"
TALOS_CLUSTER_FILE = ROOT / "kubernetes/talos/cluster.json"
SENSITIVE_VALUES = []


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
                if "HTTP 404" in str(exc):
                    raise RuntimeError(f"Required Infisical secret {path}/{name} was not found (HTTP 404)") from None
                raise RuntimeError(f"Could not read Infisical secret {path}/{name}: {exc}") from None
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


def remote(client, command, timeout=900, operation="operation"):
    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        stdin.close()  # Never leave an SSH command waiting for interactive input.
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        status = stdout.channel.recv_exit_status()
    except Exception:
        raise RuntimeError("Proxmox SSH operation failed; remote output was suppressed") from None
    safe_output = "\n".join(part.strip() for part in (out, err) if part.strip())
    for secret in sorted((value for value in SENSITIVE_VALUES if value), key=len, reverse=True):
        safe_output = safe_output.replace(secret, "[REDACTED]")
    safe_output = re.sub(
        r"(?i)\b(password|passphrase|token|secret)(\s*[:=]\s*)([^\s,;]+)",
        r"\1\2[REDACTED]",
        safe_output,
    )
    if len(safe_output) > 12000:
        safe_output = safe_output[-12000:]
    if status:
        detail = f"; Proxmox output: {safe_output}" if safe_output else "; Proxmox returned no diagnostic output"
        raise RuntimeError(f"Proxmox {operation} failed with exit status {status}{detail}")
    safe_stderr = err.strip()
    warning_pattern = re.compile(r"^user config - ignore invalid acl token .+$")
    stdout_lines = []
    for line in out.splitlines():
        if warning_pattern.fullmatch(line.strip()):
            safe_stderr = "\n".join(part for part in (safe_stderr, line.strip()) if part)
        else:
            stdout_lines.append(line)
    if safe_stderr:
        for secret in sorted((value for value in SENSITIVE_VALUES if value), key=len, reverse=True):
            safe_stderr = safe_stderr.replace(secret, "[REDACTED]")
        safe_stderr = re.sub(
            r"(?i)\b(password|passphrase|token|secret)(\s*[:=]\s*)([^\s,;]+)",
            r"\1\2[REDACTED]",
            safe_stderr,
        )
        print(f"WARNING: Proxmox stderr: {safe_stderr}", file=sys.stderr)
    return "\n".join(stdout_lines).strip()


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


def jenkins_agent_config(client):
    status = remote(client, f"pct status {JENKINS_AGENT_CTID}", timeout=30)
    if not re.search(r"(?m)^status: running\s*$", status):
        raise RuntimeError(f"Jenkins agent CTID {JENKINS_AGENT_CTID} must be running to update its DNS safely")
    config_text = remote(client, f"pct config {JENKINS_AGENT_CTID}", timeout=30)
    lines = {}
    for line in config_text.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            lines[key.strip()] = value.strip()
    network = {}
    for item in lines.get("net0", "").split(","):
        if "=" in item:
            key, value = item.strip().split("=", 1)
            network[key] = value
    tags = {tag.strip() for tag in lines.get("tags", "").split(";") if tag.strip()}
    identity_matches = (
        lines.get("hostname") == "jenkins-agent"
        and network.get("hwaddr", "").upper() == JENKINS_AGENT_MAC
        and network.get("bridge") == "vmbr0"
        and network.get("ip") == "dhcp"
        and network.get("tag") == "20"
        and "jenkins-agent" in tags
    )
    if not identity_matches:
        raise RuntimeError(
            f"CTID {JENKINS_AGENT_CTID} does not match the expected Jenkins agent identity; refusing to change its DNS"
        )
    guest_addresses = remote(
        client,
        f"pct exec {JENKINS_AGENT_CTID} -- ip -o -4 addr show scope global",
        timeout=30,
    )
    if f"inet {JENKINS_AGENT_IPV4}/" not in guest_addresses:
        raise RuntimeError(
            f"CTID {JENKINS_AGENT_CTID} does not currently hold expected address {JENKINS_AGENT_IPV4}; refusing to change its DNS"
        )
    return lines


def configure_jenkins_agent_dns(client):
    """Persist and activate the private DNS resolvers without rebooting the running agent."""
    config = jenkins_agent_config(client)
    resolver_list = ",".join(JENKINS_AGENT_NAMESERVERS)
    remote(
        client,
        f"pct set {JENKINS_AGENT_CTID} --searchdomain {shlex.quote(JENKINS_AGENT_SEARCHDOMAIN)} "
        f"--nameserver {shlex.quote(resolver_list)}",
        timeout=60,
        operation="save Jenkins agent DNS settings",
    )
    resolv_content = [
        "# --- BEGIN PVE ---",
        f"search {JENKINS_AGENT_SEARCHDOMAIN}",
        *(f"nameserver {server}" for server in JENKINS_AGENT_NAMESERVERS),
        "# --- END PVE ---",
    ]
    guest_command = "test -f /etc/resolv.conf && test ! -L /etc/resolv.conf && printf '%s\\n' " + " ".join(
        shlex.quote(line) for line in resolv_content
    ) + " > /etc/resolv.conf"
    remote(
        client,
        f"pct exec {JENKINS_AGENT_CTID} -- bash -lc {shlex.quote(guest_command)}",
        timeout=60,
        operation="activate Jenkins agent DNS settings",
    )
    resolv_conf = remote(client, f"pct exec {JENKINS_AGENT_CTID} -- cat /etc/resolv.conf", timeout=30)
    expected_lines = [f"nameserver {server}" for server in JENKINS_AGENT_NAMESERVERS]
    if any(line not in resolv_conf.splitlines() for line in expected_lines):
        raise RuntimeError("Jenkins agent resolver file did not retain both Technitium DNS servers")
    result = remote(
        client,
        f"pct exec {JENKINS_AGENT_CTID} -- getent ahostsv4 {PROXMOX_API_HOSTNAME}",
        timeout=30,
        operation="verify Jenkins agent DNS resolution",
    )
    if not re.search(rf"(?m)^{re.escape(PROXMOX_API_IPV4)}\s", result):
        raise RuntimeError(
            f"Jenkins agent DNS does not resolve {PROXMOX_API_HOSTNAME} to {PROXMOX_API_IPV4} after the update"
        )
    print(
        f"Jenkins agent CTID {JENKINS_AGENT_CTID} now uses Technitium DNS "
        f"{', '.join(JENKINS_AGENT_NAMESERVERS)} and resolves {PROXMOX_API_HOSTNAME} -> {PROXMOX_API_IPV4}."
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


def destroy_verified_container(client, ctid, config):
    """Stop and destroy an already identity-verified container, honoring protection."""
    protected = re.search(r"(?m)^protection:\s*(?:1|yes)\s*$", config) is not None
    if protected:
        print(f"CTID {ctid} is protected; temporarily disabling protection before deletion.")
        remote(client, f"pct set {ctid} --protection 0", timeout=60)

    try:
        status = remote(client, f"pct status {ctid}", timeout=30)
        if "status: running" in status:
            remote(client, f"pct stop {ctid}", timeout=120, operation=f"stop of CTID {ctid}")
    except Exception as exc:
        if protected:
            try:
                remote(client, f"pct set {ctid} --protection 1", timeout=60)
            except Exception:
                print(f"WARNING: could not restore protection on CTID {ctid} after stop failure.", file=sys.stderr)
        raise RuntimeError(f"Could not stop CTID {ctid}; protection was restored if it had been enabled. {exc}") from None

    destroy_error = None
    try:
        remote(client, f"pct destroy {ctid} -f", timeout=600, operation=f"destruction of CTID {ctid}")
    except Exception as exc:
        destroy_error = str(exc)

    try:
        still_present = remote(
            client,
            f"if pct status {ctid} >/dev/null 2>&1; then printf present; else printf absent; fi",
            timeout=30,
        )
    except Exception as exc:
        raise RuntimeError(f"Could not verify whether CTID {ctid} was deleted. {exc}") from None
    if still_present != "absent":
        if protected:
            try:
                remote(client, f"pct set {ctid} --protection 1", timeout=60)
            except Exception:
                print(f"WARNING: could not restore protection on CTID {ctid} after destroy failure.", file=sys.stderr)
        detail = f" Proxmox reported: {destroy_error}" if destroy_error else ""
        raise RuntimeError(
            f"CTID {ctid} still exists after the destroy request; protection was restored if it had been enabled.{detail}"
        )
    if destroy_error:
        print(f"Proxmox reported an error, but CTID {ctid} is confirmed absent: {destroy_error}")
    print(f"CTID {ctid} was deleted.")


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


def talos_dns_records():
    try:
        data = json.loads(TALOS_CLUSTER_FILE.read_text(encoding="utf-8"))
        nodes = data["nodes"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        raise RuntimeError("Could not read kubernetes/talos/cluster.json for DNS records") from None
    if not isinstance(nodes, list) or not nodes:
        raise RuntimeError("Talos inventory must contain at least one node before DNS deployment")
    records, names, addresses = [], set(), set()
    for node in nodes:
        if not isinstance(node, dict):
            raise RuntimeError("Talos inventory contains an invalid DNS node")
        name = str(node.get("name", "")).strip().lower()
        try:
            address = str(ipaddress.IPv4Address(node.get("ip", "")))
        except ipaddress.AddressValueError:
            raise RuntimeError(f"Talos DNS node {name or 'unnamed'} has an invalid IPv4 address") from None
        if (not re.fullmatch(r"talos-[a-z0-9-]+", name)
                or ipaddress.IPv4Address(address) not in ipaddress.ip_network("192.168.80.0/24")
                or name in names or address in addresses):
            raise RuntimeError(f"Talos DNS inventory entry for {name or 'unnamed'} is invalid or duplicated")
        names.add(name); addresses.add(address)
        records.append({"hostname": f"{name}.fouchger.uk", "address": address})
    return records


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

    execute_in_ct(client, 150, f"{stage}/manage-technitium.py", {
        "admin_password": values["DNS01_ADMIN_PASSWORD"],
        "action": "ensure-proxmox-api-record",
        "hostname": "pve01.fouchger.uk",
        "address": "192.168.20.10",
    }, "ensure-proxmox-api-record")

    talos_records = talos_dns_records()
    for record in talos_records:
        execute_in_ct(client, 150, f"{stage}/manage-technitium.py", {
            "admin_password": values["DNS01_ADMIN_PASSWORD"],
            "action": "ensure-a-record",
            "hostname": record["hostname"],
            "address": record["address"],
            "comments": "Managed by Homelab Jenkins Job 006 from kubernetes/talos/cluster.json",
        }, f"ensure-{record['hostname']}")

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
    execute_in_ct(client, 151, f"{stage}/manage-technitium.py", {
        "admin_password": values["DNS02_ADMIN_PASSWORD"],
        "action": "verify-proxmox-api-record",
        "hostname": "pve01.fouchger.uk",
        "address": "192.168.20.10",
        "zone_type": "Secondary",
    }, "verify-proxmox-api-record")
    for record in talos_records:
        execute_in_ct(client, 151, f"{stage}/manage-technitium.py", {
            "admin_password": values["DNS02_ADMIN_PASSWORD"],
            "action": "verify-a-record",
            "hostname": record["hostname"],
            "address": record["address"],
            "zone_type": "Secondary",
        }, f"verify-{record['hostname']}")
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
                print(f"RECREATING {role} (CTID {ctid}); hosted zone data will be preserved before deletion.")
                try:
                    peer_role = "dns02" if role == "dns01" else "dns01"
                    print(f"Verifying {peer_role} is healthy before taking {role} offline.")
                    ensure_peer_dns_online(client, stage, peer_role, values)
                    if role == "dns01":
                        preserved_dns01_zones = export_zone_files(client, stage, "dns01", values, ["Primary"])
                    destroy_verified_container(client, ctid, existing)
                except Exception as exc:
                    try:
                        final_status = remote(client, f"pct status {ctid}", timeout=30).replace("status: ", "").strip()
                    except Exception:
                        final_status = "unknown (Proxmox status query failed)"
                    raise RuntimeError(
                        f"Could not safely remove {role} CTID {ctid}; its current status is {final_status}. "
                        f"Proxmox reported: {exc}"
                    ) from None
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
    configure_jenkins_agent_dns(client)

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
                print(f"- {role}: DESTRUCTIVE — delete identity-verified CTID {ctid} ({state}) and create a clean replacement at {expected_ip}; {other_role} stays online during this recreation; Proxmox protection is handled safely")
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
    print("- Technitium dns01: ensure pve01.fouchger.uk A record -> 192.168.20.10; replicate its zone to dns02")
    talos_records = talos_dns_records()
    print(f"- Technitium dns01: ensure {len(talos_records)} Talos node A records from kubernetes/talos/cluster.json; verify all records on dns02")
    agent_config = jenkins_agent_config(client)
    current_nameservers = agent_config.get("nameserver", "(inherit from Proxmox host)")
    current_searchdomain = agent_config.get("searchdomain", "(inherit from Proxmox host)")
    print(
        f"- Jenkins agent CTID {JENKINS_AGENT_CTID}: DNS settings "
        f"[{current_nameservers}; search {current_searchdomain}] -> "
        f"[{','.join(JENKINS_AGENT_NAMESERVERS)}; search {JENKINS_AGENT_SEARCHDOMAIN}]; "
        "apply to PVE config and live resolver without reboot"
    )
    print("- Router DNS and DHCP settings: reviewed separately below; the apply stage creates an encrypted backup before changes.")


def main():
    arguments = sys.argv[1:]
    if arguments not in ([], ["--plan"]):
        raise RuntimeError("Usage: deploy-dns.py [--plan]")
    recreate_roles = selected_recreate_roles()
    values = read_infisical()
    SENSITIVE_VALUES.extend(value for value in values.values() if isinstance(value, str) and value)
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
