#!/usr/bin/env python3
"""Read-only connection and change review for the configured MikroTik router."""

import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

PROXMOX_API_DNS_NAME = "pve01.fouchger.uk"
PROXMOX_API_DNS_ADDRESS = "192.168.20.10"
PROXMOX_API_DNS_COMMENT = "homelab-managed-proxmox-api"
TALOS_CLUSTER_FILE = Path(__file__).resolve().parents[3] / "kubernetes/talos/cluster.json"
TALOS_DOMAIN = "fouchger.uk"
TALOS_DHCP_SERVER = "dhcp-vlan80"


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required Jenkins setting {name} is missing")
    return value


def api_json(url, method="GET", token=None, form=None):
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
        if exc.code == 404:
            return None
        raise RuntimeError(f"Infisical request failed with HTTP {exc.code}; response was suppressed") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Infisical request failed: {type(exc).__name__}") from None
    try:
        return json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("Infisical returned invalid JSON") from None


def infisical_values():
    base = required("INFISICAL_URL").rstrip("/")
    if not base.startswith("https://"):
        raise RuntimeError("INFISICAL_URL must use HTTPS")
    login = api_json(
        f"{base}/api/v1/auth/universal-auth/login", "POST",
        form={"clientId": required("INFISICAL_READ_CLIENT_ID"),
              "clientSecret": required("INFISICAL_READ_CLIENT_SECRET")},
    )
    token = login.get("accessToken") if isinstance(login, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError("Infisical read identity did not authenticate")
    result = {}
    paths = {
        "/mikrotik/router01": (
            "MIKROTIK_HOST", "MIKROTIK_IP", "MIKROTIK_USERNAME", "MIKROTIK_PASSWORD",
            "MIKROTIK_BOOTSTRAP_USERNAME", "MIKROTIK_BOOTSTRAP_PASSWORD",
            "MIKROTIK_TLS_CERT_SHA256", "MIKROTIK_SSH_USER", "MIKROTIK_SSH_PRIVATE_KEY",
            "MIKROTIK_SSH_PUBLIC_KEY", "MIKROTIK_SSH_HOST_KEY",
        ),
        "/dns": ("MIKROTIK_DHCP_DNS_MODE", "DNS_PUBLIC_FALLBACKS"),
        "/dns/dns01": ("DNS_IPV4",),
        "/dns/dns02": ("DNS_IPV4",),
        "/mikrotik/backup": ("BINARY_BACKUP_PASSWORD",),
        "/mikrotik/wifi_security": (
            "SEC_USERS_PASSWORD", "SEC_MGMT_PASSWORD", "SEC_IOT_PASSWORD", "SEC_GUEST_PASSWORD",
        ),
    }
    for path, names in paths.items():
        query = urllib.parse.urlencode({
            "projectId": required("INFISICAL_PROJECT_ID"),
            "environment": required("INFISICAL_ENVIRONMENT"),
            "secretPath": path,
        })
        for name in names:
            record = api_json(
                f"{base}/api/v4/secrets/{urllib.parse.quote(name)}?{query}", token=token,
            )
            secret = record.get("secret", {}) if isinstance(record, dict) else {}
            value = secret.get("secretValue") if isinstance(secret, dict) else None
            if isinstance(value, str):
                key = f"{path.rsplit('/', 1)[-1].upper()}_IPV4" if name == "DNS_IPV4" else name
                result[key] = value
    return result


class RouterREST:
    def __init__(self, values):
        self.host = values.get("MIKROTIK_HOST", "").strip()
        self.fallback_host = values.get("MIKROTIK_IP", "").strip()
        self.fingerprint = values.get("MIKROTIK_TLS_CERT_SHA256", "").lower().replace(":", "").strip()
        self.username = values.get("MIKROTIK_USERNAME", "").strip()
        self.password = values.get("MIKROTIK_PASSWORD", "")
        if not self.username or not self.password:
            self.username = values.get("MIKROTIK_BOOTSTRAP_USERNAME", "").strip()
            self.password = values.get("MIKROTIK_BOOTSTRAP_PASSWORD", "")
        if not re.fullmatch(r"[A-Za-z0-9.-]+", self.host) or self.host.startswith(".") or self.host.endswith("."):
            raise RuntimeError("Set MIKROTIK_HOST in Settings before reviewing the router")
        if not re.fullmatch(r"[0-9a-f]{64}", self.fingerprint):
            raise RuntimeError("Set the independently verified MIKROTIK_TLS_CERT_SHA256 in Settings before connecting")
        if not self.username or not self.password:
            raise RuntimeError("Set a RouterOS REST username and password in Settings before connecting")
        self.auth = "Basic " + base64.b64encode(f"{self.username}:{self.password}".encode()).decode("ascii")

    def get(self, path):
        endpoints = [self.host]
        if self.fallback_host and self.fallback_host != self.host:
            endpoints.append(self.fallback_host)
        for index, endpoint in enumerate(endpoints):
            connection = http.client.HTTPSConnection(
                endpoint, 443, timeout=20, context=ssl._create_unverified_context(),
            )
            try:
                connection.connect()
                peer = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
                if not hmac.compare_digest(peer, self.fingerprint):
                    raise RuntimeError("Router HTTPS certificate does not match the saved trust pin")
                connection.request("GET", "/rest/" + path, headers={
                    "Authorization": self.auth, "Accept": "application/json",
                })
                response = connection.getresponse()
                raw = response.read()
                if response.status >= 400:
                    raise RuntimeError(f"RouterOS read request failed with HTTP {response.status}; response was suppressed")
                try:
                    return json.loads(raw.decode("utf-8")) if raw else {}
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise RuntimeError("RouterOS returned invalid JSON") from None
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                if index == 0 and len(endpoints) > 1 and isinstance(exc, ConnectionRefusedError):
                    print(f"HTTPS on MIKROTIK_HOST={self.host} refused the connection; trying MIKROTIK_IP={self.fallback_host} with the same saved certificate pin.")
                    continue
                raise RuntimeError(f"Router HTTPS connection failed at {endpoint} ({type(exc).__name__})") from None
            finally:
                connection.close()
        raise RuntimeError(f"Router HTTPS connection failed at MIKROTIK_HOST={self.host} and MIKROTIK_IP={self.fallback_host}; no router changes were made")


def valid_ipv4(value, name):
    try:
        return str(ipaddress.IPv4Address(value.strip()))
    except (ValueError, AttributeError):
        raise RuntimeError(f"{name} must be a valid IPv4 address before Review") from None


def talos_inventory():
    try:
        nodes = json.loads(TALOS_CLUSTER_FILE.read_text(encoding="utf-8"))["nodes"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        raise RuntimeError("Could not read kubernetes/talos/cluster.json for the Talos network plan") from None
    if not isinstance(nodes, list) or not nodes:
        raise RuntimeError("Talos inventory must contain at least one node")
    result, seen_names, seen_macs, seen_ips = [], set(), set(), set()
    for item in nodes:
        if not isinstance(item, dict):
            raise RuntimeError("Talos inventory contains an invalid node")
        name, mac = str(item.get("name", "")).strip().lower(), str(item.get("mac", "")).strip().upper()
        address = valid_ipv4(str(item.get("ip", "")), f"{name or 'Talos node'} IP")
        if (not re.fullmatch(r"talos-[a-z0-9-]+", name)
                or not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", mac)
                or ipaddress.IPv4Address(address) not in ipaddress.ip_network("192.168.80.0/24")
                or name in seen_names or mac in seen_macs or address in seen_ips):
            raise RuntimeError(f"Talos inventory entry for {name or 'a node'} has invalid or duplicate data")
        seen_names.add(name); seen_macs.add(mac); seen_ips.add(address)
        result.append({"name": name, "mac": mac, "ip": address,
                       "hostname": f"{name}.{TALOS_DOMAIN}", "comment": f"homelab-managed-{name}"})
    return result


def ssh_backup_blocker(values):
    host = values.get("MIKROTIK_HOST", "").strip()
    username = values.get("MIKROTIK_SSH_USER", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", username):
        return "set MIKROTIK_SSH_USER"
    if not values.get("MIKROTIK_SSH_PRIVATE_KEY", "").startswith("-----BEGIN OPENSSH PRIVATE KEY-----"):
        return "run Settings to generate MIKROTIK_SSH_PRIVATE_KEY"
    public_key = values.get("MIKROTIK_SSH_PUBLIC_KEY", "").strip().split()
    if len(public_key) < 3 or public_key[0] != "ssh-ed25519":
        return "run Settings to generate MIKROTIK_SSH_PUBLIC_KEY"
    lines = [line.strip() for line in values.get("MIKROTIK_SSH_HOST_KEY", "").splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    if len(lines) != 1:
        return "run Job 003 Settings with SSH host-key update set to yes; it verifies and saves the key over pinned HTTPS"
    fields = lines[0].split()
    if len(fields) < 3 or fields[0] != host or not fields[1].startswith("ssh-"):
        return "run Job 003 Settings with SSH host-key update set to yes to verify and refresh the saved key"
    return ""


def review(values, router):
    resources = router.get("system/resource")
    identity = router.get("system/identity")
    dns = router.get("ip/dns")
    static_dns = router.get("ip/dns/static")
    networks = router.get("ip/dhcp-server/network")
    bridge_vlans = router.get("interface/bridge/vlan")
    dhcp_servers = router.get("ip/dhcp-server")
    leases = router.get("ip/dhcp-server/lease")
    if isinstance(resources, list):
        resources = resources[0] if resources else {}
    if isinstance(identity, list):
        identity = identity[0] if identity else {}
    if isinstance(dns, list):
        dns = dns[0] if dns else {}
    if not isinstance(resources, dict) or not isinstance(identity, dict) or not isinstance(dns, dict):
        raise RuntimeError("RouterOS returned incomplete system or DNS details")
    if not isinstance(networks, list):
        raise RuntimeError("RouterOS returned an invalid DHCP network list")
    if not isinstance(static_dns, list):
        raise RuntimeError("RouterOS returned an invalid static DNS record list")
    if not all(isinstance(rows, list) for rows in (bridge_vlans, dhcp_servers, leases)):
        raise RuntimeError("RouterOS returned invalid VLAN or DHCP data")

    print("Router connection: verified (HTTPS certificate pin and REST login)")
    print(f"Router: {identity.get('name', 'name unavailable')} | RouterOS {resources.get('version', 'version unavailable')}")
    ssh_blocker = ssh_backup_blocker(values)
    if ssh_blocker:
        print(f"Router backup connection: blocked; {ssh_blocker}")
    else:
        print("Router backup settings: present (pinned SSH host key; Job 004 will authorize and verify Jenkins SSH access after approval)")
    backup_password = values.get("BINARY_BACKUP_PASSWORD", "")
    if len(backup_password) < 8 or any(char in backup_password for char in "\r\n\0"):
        print("Router backup encryption: blocked; set BINARY_BACKUP_PASSWORD (at least 8 characters) in Settings")
    else:
        print("Router backup encryption: ready")

    desired = []
    missing_dns = []
    for key in ("DNS01_IPV4", "DNS02_IPV4"):
        if values.get(key, "").strip():
            desired.append(valid_ipv4(values[key], key))
        else:
            missing_dns.append(key)
    fallbacks = [item.strip() for item in values.get("DNS_PUBLIC_FALLBACKS", "").split(",") if item.strip()]
    normalized_fallbacks = []
    for item in fallbacks:
        try:
            normalized_fallbacks.append(str(ipaddress.ip_address(item)))
        except ValueError:
            raise RuntimeError("DNS_PUBLIC_FALLBACKS contains an invalid address") from None
    desired.extend(normalized_fallbacks)
    mode = values.get("MIKROTIK_DHCP_DNS_MODE", "").strip().lower()

    current_dns = [item.strip() for item in str(dns.get("servers", "")).split(",") if item.strip()]
    wanted_dns = ",".join(desired)
    current_dns_text = ",".join(current_dns) or "(none)"
    if missing_dns:
        print("DNS upstream plan: blocked; missing " + ", ".join(missing_dns))
    elif desired:
        print(f"DNS upstreams: {'matches' if current_dns == desired else 'change planned'} | current [{current_dns_text}] -> planned [{wanted_dns}]")
    else:
        print("DNS upstream plan: no resolvers configured")
    if mode in ("router", "direct"):
        wanted_remote = "true" if mode == "router" else "false"
        print(f"Router DNS service: {'matches' if dns.get('allow-remote-requests') == wanted_remote else 'change planned'} | mode {mode}")
    else:
        print("Router DNS service: blocked; set MIKROTIK_DHCP_DNS_MODE to router or direct")

    pve_records = [
        row for row in static_dns
        if isinstance(row, dict)
        and str(row.get("name", "")).rstrip(".").lower() == PROXMOX_API_DNS_NAME
        and str(row.get("disabled", "false")).lower() != "true"
    ]
    pve_conflicts = [
        row for row in pve_records
        if str(row.get("address", "")) != PROXMOX_API_DNS_ADDRESS
        and str(row.get("comment", "")) != PROXMOX_API_DNS_COMMENT
    ]
    if pve_conflicts:
        raise RuntimeError(
            f"RouterOS has a conflicting unmanaged DNS entry for {PROXMOX_API_DNS_NAME}; "
            "resolve it before applying the Proxmox API DNS record"
        )
    pve_record_matches = any(
        str(row.get("address", "")) == PROXMOX_API_DNS_ADDRESS for row in pve_records
    )
    managed_pve_conflicts = any(
        str(row.get("comment", "")) == PROXMOX_API_DNS_COMMENT
        and str(row.get("address", "")) != PROXMOX_API_DNS_ADDRESS
        for row in pve_records
    )
    if managed_pve_conflicts:
        pve_record_matches = False
    print(
        f"Proxmox API DNS: {'matches' if pve_record_matches else 'change planned'} | "
        f"{PROXMOX_API_DNS_NAME} -> {PROXMOX_API_DNS_ADDRESS}"
    )

    talos = talos_inventory()
    vlan80 = [row for row in bridge_vlans if isinstance(row, dict) and str(row.get("vlan-ids", "")) == "80"]
    vlan_match = (len(vlan80) == 1 and vlan80[0].get("bridge") == "bridge"
                  and vlan80[0].get("tagged", "").replace(" ", "") == "bridge,ether2"
                  and vlan80[0].get("untagged", "") in ("", "none"))
    vlan_blocked = len(vlan80) > 1 or (len(vlan80) == 1 and not vlan_match
                                       and vlan80[0].get("comment") != "Kubernetes trunk")
    print(f"Talos VLAN 80 trunk: {'blocked by duplicate/unmanaged membership' if vlan_blocked else 'matches' if vlan_match else 'change planned to tagged bridge,ether2'}")
    active_servers = [row for row in dhcp_servers if isinstance(row, dict)
                      and row.get("name") == TALOS_DHCP_SERVER
                      and str(row.get("disabled", "false")).lower() != "true"]
    print(f"Talos DHCP server: {'ready' if len(active_servers) == 1 else 'blocked; expected exactly one active dhcp-vlan80'}")
    lease_conflict = False
    dns_conflict = False
    lease_updates = 0
    for node in talos:
        matches = [row for row in leases if isinstance(row, dict)
                   and str(row.get("mac-address", "")).upper() == node["mac"]]
        conflicting_ip = any(isinstance(row, dict) and row.get("address") == node["ip"]
                             and str(row.get("mac-address", "")).upper() != node["mac"] for row in leases)
        if len(matches) > 1 or conflicting_ip:
            lease_conflict = True
            print(f"Talos DHCP reservation {node['name']}: blocked by duplicate MAC or address conflict")
        else:
            match = matches[0] if matches else None
            matches_expected = bool(match and match.get("address") == node["ip"]
                                    and match.get("server") == TALOS_DHCP_SERVER
                                    and str(match.get("dynamic", "false")).lower() != "true")
            lease_updates += 0 if matches_expected else 1
            print(f"Talos DHCP reservation {node['name']}: {'matches' if matches_expected else 'change planned'} | {node['mac']} -> {node['ip']}")
        same_name = [row for row in static_dns if isinstance(row, dict)
                     and str(row.get("name", "")).rstrip(".").lower() == node["hostname"]
                     and str(row.get("disabled", "false")).lower() != "true"]
        if any(row.get("address") != node["ip"] and row.get("comment") != node["comment"] for row in same_name):
            dns_conflict = True
            print(f"Talos router DNS {node['hostname']}: blocked by conflicting unmanaged record")
        else:
            exact = any(row.get("address") == node["ip"] for row in same_name)
            print(f"Talos router DNS {node['hostname']}: {'matches' if exact else 'change planned'} -> {node['ip']}")
    if vlan_blocked or len(active_servers) != 1 or lease_conflict or dns_conflict:
        raise RuntimeError("Resolve the Talos VLAN, DHCP, or DNS blockers before approving RouterOS changes")
    print(f"Talos network plan: VLAN 80 plus {lease_updates} DHCP reservation update(s) and managed router DNS for {len(talos)} nodes")

    networks_with_dns = []
    for item in networks:
        if not isinstance(item, dict) or item.get("disabled") == "true":
            continue
        networks_with_dns.append(item)
    print(f"DHCP networks: {len(networks_with_dns)} active network(s) found")
    mismatch = 0
    for item in networks_with_dns:
        gateway = item.get("gateway", "")
        if mode in ("router", "direct") and not missing_dns:
            expected = gateway if mode == "router" else wanted_dns
            actual = str(item.get("dns-server", "")).replace(" ", "")
            if actual != expected.replace(" ", ""):
                mismatch += 1
    if mode in ("router", "direct") and not missing_dns:
        print(f"DHCP DNS: {mismatch} network(s) need an update")
    else:
        print("DHCP DNS: review blocked until required DNS settings are saved")

    if mode in ("router", "direct"):
        firewall = router.get("ip/firewall/filter")
        if not isinstance(firewall, list):
            raise RuntimeError("RouterOS returned an invalid firewall filter list")
        prefix = "homelab-managed-router-dns-"
        desired_rules = {}
        if mode == "router":
            for item in networks_with_dns:
                try:
                    network = str(ipaddress.ip_network(item.get("address", ""), strict=True))
                except ValueError:
                    continue
                tag = re.sub(r"[^A-Za-z0-9.-]", "-", network)
                comment = prefix + tag
                for protocol in ("udp", "tcp"):
                    desired_rules[(comment, protocol)] = {
                        "chain": "input", "action": "accept", "protocol": protocol,
                        "dst-port": "53", "src-address": network,
                        "comment": comment,
                    }
        kept_rules = set()
        remove_count = 0
        for rule in firewall:
            if not isinstance(rule, dict) or not str(rule.get("comment", "")).startswith(prefix):
                continue
            key = (str(rule.get("comment", "")), str(rule.get("protocol", "")))
            expected = desired_rules.get(key)
            valid = bool(expected) and all(str(rule.get(field, "")) == value for field, value in expected.items())
            valid = valid and str(rule.get("disabled", "false")).lower() != "true"
            if valid and key not in kept_rules:
                kept_rules.add(key)
            else:
                remove_count += 1
        add_count = len(set(desired_rules) - kept_rules)
        if add_count or remove_count:
            print(f"Managed router DNS firewall rules: {add_count} to add, {remove_count} to remove")
        else:
            print("Managed router DNS firewall rules: matches")

    profiles = {"SEC_USERS_PASSWORD": "sec-users", "SEC_MGMT_PASSWORD": "sec-mgmt",
                "SEC_IOT_PASSWORD": "sec-iot", "SEC_GUEST_PASSWORD": "sec-guest"}
    requested = [profile for key, profile in profiles.items() if values.get(key)]
    if requested:
        wifi = router.get("interface/wifi/security")
        if not isinstance(wifi, list):
            raise RuntimeError("RouterOS returned an invalid Wi-Fi security profile list")
        for profile in requested:
            if sum(1 for row in wifi if isinstance(row, dict) and row.get("name") == profile) != 1:
                raise RuntimeError(f"RouterOS must contain exactly one Wi-Fi security profile named {profile}")
        print("Wi-Fi: password updates configured for " + ", ".join(requested) + " (current passwords are not readable for comparison)")
    else:
        print("Wi-Fi: no password updates configured")
    if os.environ.get("HOMELAB_REQUIRE_MIKROTIK_REVIEW", "").strip().lower() in ("1", "yes", "true"):
        if missing_dns:
            raise RuntimeError("Set both DNS_IPV4 values in Infisical before approving router changes")
        if mode not in ("router", "direct"):
            raise RuntimeError("Set MIKROTIK_DHCP_DNS_MODE to router or direct in Settings before approving router changes")
        ssh_blocker = ssh_backup_blocker(values)
        if ssh_blocker:
            raise RuntimeError("Complete the router SSH backup settings before approving changes: " + ssh_blocker)
        backup_password = values.get("BINARY_BACKUP_PASSWORD", "")
        if len(backup_password) < 8 or any(char in backup_password for char in "\r\n\0"):
            raise RuntimeError("Set BINARY_BACKUP_PASSWORD in Settings before approving router changes")
        for key in profiles:
            password = values.get(key, "")
            if password and (len(password) < 8 or any(char in password for char in "\r\n\0")):
                raise RuntimeError(f"{key} must be at least 8 characters and contain no line breaks")
    print("Review is read-only; no router settings were changed.")


if __name__ == "__main__":
    try:
        secrets = infisical_values()
        missing = []
        if not secrets.get("MIKROTIK_HOST", "").strip():
            missing.append("MIKROTIK_HOST")
        if not secrets.get("MIKROTIK_TLS_CERT_SHA256", "").strip():
            missing.append("MIKROTIK_TLS_CERT_SHA256 (independently verified)")
        if not ((secrets.get("MIKROTIK_USERNAME", "").strip() and secrets.get("MIKROTIK_PASSWORD", ""))
                or (secrets.get("MIKROTIK_BOOTSTRAP_USERNAME", "").strip()
                    and secrets.get("MIKROTIK_BOOTSTRAP_PASSWORD", ""))):
            missing.append("RouterOS REST username and password")
        if missing:
            print("Router connection: not ready; missing " + ", ".join(missing))
            print("No router connection was attempted and no settings were changed.")
            if os.environ.get("HOMELAB_REQUIRE_MIKROTIK_REVIEW", "").strip().lower() in ("1", "yes", "true"):
                raise RuntimeError("Complete Settings before approving router changes")
            sys.exit(0)
        review(secrets, RouterREST(secrets))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
