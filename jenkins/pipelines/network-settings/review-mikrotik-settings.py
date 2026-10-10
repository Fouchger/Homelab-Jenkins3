#!/usr/bin/env python3
"""Read-only connection and change review for the configured MikroTik router."""

import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request


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
            "MIKROTIK_HOST", "MIKROTIK_USERNAME", "MIKROTIK_PASSWORD",
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
        connection = http.client.HTTPSConnection(
            self.host, 443, timeout=20, context=ssl._create_unverified_context(),
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
            raise RuntimeError(f"Router HTTPS connection failed ({type(exc).__name__})") from None
        finally:
            connection.close()


def valid_ipv4(value, name):
    try:
        return str(ipaddress.IPv4Address(value.strip()))
    except (ValueError, AttributeError):
        raise RuntimeError(f"{name} must be a valid IPv4 address before Review") from None


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
        return "verify and save MIKROTIK_SSH_HOST_KEY in Settings"
    fields = lines[0].split()
    if len(fields) < 3 or fields[0] != host or not fields[1].startswith("ssh-"):
        return "saved MIKROTIK_SSH_HOST_KEY does not match MIKROTIK_HOST"
    return ""


def review(values, router):
    resources = router.get("system/resource")
    identity = router.get("system/identity")
    dns = router.get("ip/dns")
    networks = router.get("ip/dhcp-server/network")
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
            raise RuntimeError("Set both DNS_IPV4 values in Settings before approving router changes")
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
