#!/usr/bin/env python3
"""Apply Infisical-managed DNS and Wi-Fi settings to RouterOS after an encrypted backup."""

import ipaddress
import http.client
import hashlib
import hmac
import base64
import json
import os
import re
import ssl
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import paramiko
from paramiko.hostkeys import HostKeyEntry


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required Jenkins setting {name} is missing")
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


def infisical_secrets():
    base_url = required("INFISICAL_URL").rstrip("/")
    if not base_url.startswith("https://"):
        raise RuntimeError("INFISICAL_URL must use HTTPS")
    login = api_json(
        f"{base_url}/api/v1/auth/universal-auth/login",
        method="POST",
        form={
            "clientId": required("INFISICAL_READ_CLIENT_ID"),
            "clientSecret": required("INFISICAL_READ_CLIENT_SECRET"),
        },
    )
    token = login.get("accessToken") if isinstance(login, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError("Infisical read identity did not authenticate")

    values = {}
    for path, names in {
        "/dns": (
            "DNS_PUBLIC_FALLBACKS", "MIKROTIK_DHCP_DNS_MODE",
        ),
        "/mikrotik/router01": (
            "MIKROTIK_HOST", "MIKROTIK_IP", "MIKROTIK_BOOTSTRAP_USERNAME", "MIKROTIK_BOOTSTRAP_PASSWORD",
            "MIKROTIK_SSH_USER", "MIKROTIK_SSH_PRIVATE_KEY", "MIKROTIK_SSH_PUBLIC_KEY",
            "MIKROTIK_SSH_HOST_KEY", "MIKROTIK_USERNAME", "MIKROTIK_PASSWORD", "MIKROTIK_TLS_CERT_SHA256",
        ),
        "/mikrotik/backup": ("BINARY_BACKUP_PASSWORD",),
        "/mikrotik/wifi_security": ("SEC_GUEST_PASSWORD", "SEC_IOT_PASSWORD", "SEC_MGMT_PASSWORD", "SEC_USERS_PASSWORD"),
        "/dns/dns01": ("DNS_IPV4",),
        "/dns/dns02": ("DNS_IPV4",),
    }.items():
        query = urllib.parse.urlencode({
            "projectId": required("INFISICAL_PROJECT_ID"),
            "environment": required("INFISICAL_ENVIRONMENT"),
            "secretPath": path,
        })
        for name in names:
            result = api_json(
                f"{base_url}/api/v4/secrets/{urllib.parse.quote(name)}?{query}",
                token=token,
                allow_404=True,
            )
            secret = result.get("secret", {}) if isinstance(result, dict) else {}
            value = secret.get("secretValue") if isinstance(secret, dict) else None
            if isinstance(value, str):
                key = f"{path.rsplit('/', 1)[-1].upper()}_IPV4" if name == "DNS_IPV4" else name
                values[key] = value
    return values


def ros_quote(value):
    if any(char in value for char in "\r\n\0"):
        raise RuntimeError("RouterOS setting contains an unsupported line break")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def host_key_line(value, host):
    lines = [line.strip() for line in value.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if len(lines) != 1:
        raise RuntimeError("MIKROTIK_SSH_HOST_KEY is missing or invalid; run Job 003 and enter the independently verified SHA256 fingerprint in MIKROTIK_SSH_HOST_KEY_FINGERPRINT")
    fields = lines[0].split()
    if len(fields) < 3 or fields[0] != host or not fields[1].startswith("ssh-"):
        raise RuntimeError("MIKROTIK_SSH_HOST_KEY does not match MIKROTIK_HOST")
    return fields


def connection_host(values):
    """Use the management endpoint normally; only reset recovery overrides it."""
    host = os.environ.get("HOMELAB_MIKROTIK_CONNECT_HOST", "").strip()
    if not host:
        host = values.get("MIKROTIK_HOST", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host) or host.startswith(".") or host.endswith("."):
        raise RuntimeError("Set a valid MIKROTIK_HOST in /mikrotik/router01")
    return host


def run_command(client, command, *, allow_error=False):
    try:
        _stdin, stdout, stderr = client.exec_command(command, timeout=30)
        output = stdout.read().decode("utf-8", errors="replace")
        error = stderr.read().decode("utf-8", errors="replace")
        status = stdout.channel.recv_exit_status()
    except Exception:
        raise RuntimeError("RouterOS SSH command failed; command output was suppressed") from None
    if not allow_error and (status != 0 or re.search(r"(?im)^\s*(failure|error):", output + "\n" + error)):
        raise RuntimeError("RouterOS rejected a configuration command; output was suppressed")
    return output


def create_backup(client, backup_password):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_name = f"homelab-router-before-dns-{stamp}.backup"
    backup_remote_path = "/" + backup_name
    build_number = os.environ.get("BUILD_NUMBER", "").strip()
    if not re.fullmatch(r"[0-9]+", build_number):
        raise RuntimeError("Jenkins BUILD_NUMBER is missing or invalid; cannot safely archive this run's backup")
    artifact_dir = Path("artifacts") / build_number
    artifact_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(artifact_dir, 0o700)
    backup_local_path = artifact_dir / backup_name
    run_command(client, f"/system/backup/save name={backup_name} password={ros_quote(backup_password)}")
    try:
        sftp = client.open_sftp()
        try:
            try:
                sftp.get(backup_remote_path, str(backup_local_path))
            except OSError:
                sftp.get(backup_name, str(backup_local_path))
        finally:
            sftp.close()
    except Exception:
        raise RuntimeError("Encrypted RouterOS backup was created but could not be downloaded; no router changes were applied") from None
    if not backup_local_path.is_file() or backup_local_path.stat().st_size == 0:
        raise RuntimeError("RouterOS backup download was empty; no router changes were applied")
    os.chmod(backup_local_path, 0o600)
    run_command(client, f"/file/remove [find where name={ros_quote(backup_name)}]", allow_error=True)
    print(f"Encrypted pre-change backup saved as Jenkins artifact {backup_name}.")


class RouterREST:
    """RouterOS REST client with an independently pinned HTTPS certificate."""

    def __init__(self, host, username, password, fingerprint):
        if not username or not password:
            raise RuntimeError("Set MIKROTIK_USERNAME and MIKROTIK_PASSWORD or the bootstrap username/password in /mikrotik/router01 for RouterOS REST access")
        fingerprint = fingerprint.lower().replace(":", "").strip()
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise RuntimeError("Set MIKROTIK_TLS_CERT_SHA256 in /mikrotik/router01; run Job 003 to save the independently verified HTTPS certificate fingerprint")
        if not re.fullmatch(r"[A-Za-z0-9.-]+", host) or host.startswith(".") or host.endswith("."):
            raise RuntimeError("MIKROTIK_HOST must be a hostname or IPv4 address")
        self.host = host
        self.auth = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        self.fingerprint = fingerprint

    def call(self, path, method="GET", payload=None, allow_404=False):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload is not None else None
        connection = http.client.HTTPSConnection(self.host, 443, timeout=25, context=ssl._create_unverified_context())
        try:
            connection.connect()
            actual = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
            if not hmac.compare_digest(actual, self.fingerprint):
                raise RuntimeError("MikroTik HTTPS certificate does not match MIKROTIK_TLS_CERT_SHA256; no changes were applied")
            connection.request(method, "/rest/" + path.lstrip("/"), body=body, headers={
                "Authorization": self.auth,
                "Accept": "application/json",
                "Content-Type": "application/json",
            })
            response = connection.getresponse()
            raw = response.read()
            if allow_404 and response.status == 404:
                return None
            if response.status >= 400:
                raise RuntimeError(f"RouterOS REST {method} {path} failed with HTTP {response.status}; response suppressed")
            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise RuntimeError(f"RouterOS REST {method} {path} returned invalid JSON") from None
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            raise RuntimeError(f"MikroTik HTTPS REST connection failed ({type(exc).__name__})") from None
        finally:
            connection.close()


def rest_record_id(record):
    identifier = record.get(".id") if isinstance(record, dict) else None
    if not isinstance(identifier, str) or not re.fullmatch(r"\*[0-9A-Fa-f]+", identifier):
        raise RuntimeError("RouterOS REST returned an invalid record ID")
    return identifier


def rest_networks(router):
    rows = router.call("ip/dhcp-server/network")
    if not isinstance(rows, list):
        raise RuntimeError("RouterOS REST returned an invalid DHCP network list")
    networks = []
    for row in rows:
        if not isinstance(row, dict) or row.get("disabled") == "true":
            continue
        try:
            network = str(ipaddress.ip_network(row.get("address", ""), strict=True))
            gateway = str(ipaddress.IPv4Address(row.get("gateway", "")))
        except ValueError:
            continue
        networks.append((rest_record_id(row), network, gateway))
    if not networks:
        raise RuntimeError("Could not read active DHCP network and gateway records; no changes were made")
    return networks


def rest_set(router, path, payload):
    result = router.call(path, "PATCH", payload)
    if not isinstance(result, dict):
        raise RuntimeError(f"RouterOS did not confirm the update to {path}")
    return result


def rest_apply(router, values, networks, mode, dns_list, wifi_updates):
    # Validate every target before the first write to avoid a partial rollout.
    dns_state = router.call("ip/dns")
    if isinstance(dns_state, list):
        dns_state = dns_state[0] if dns_state else {}
    if not isinstance(dns_state, dict):
        raise RuntimeError("RouterOS REST returned invalid DNS settings")
    wifi_records = {}
    if wifi_updates:
        rows = router.call("interface/wifi/security")
        for profile, _password in wifi_updates:
            matches = [row for row in rows if isinstance(row, dict) and row.get("name") == profile]
            if len(matches) != 1:
                raise RuntimeError(f"RouterOS must contain exactly one Wi-Fi security profile named {profile}; no changes were made")
            wifi_records[profile] = matches[0]
    existing_rules = router.call("ip/firewall/filter")
    if not isinstance(existing_rules, list):
        raise RuntimeError("RouterOS REST returned an invalid firewall filter list")

    router.call("ip/dns/set", "POST", {
        "allow-remote-requests": "true" if mode == "router" else "false",
        "servers": dns_list,
    })

    dhcp_dns = [gateway if mode == "router" else dns_list for _identifier, _network, gateway in networks]
    for (identifier, network, _gateway), server_list in zip(networks, dhcp_dns):
        rest_set(router, f"ip/dhcp-server/network/{identifier}", {"dns-server": server_list})

    for rule in existing_rules:
        if isinstance(rule, dict) and str(rule.get("comment", "")).startswith("homelab-managed-router-dns-"):
            router.call(f"ip/firewall/filter/{rest_record_id(rule)}", "DELETE")
    if mode == "router":
        for _identifier, network, _gateway in networks:
            tag = re.sub(r"[^A-Za-z0-9.-]", "-", network)
            for protocol in ("udp", "tcp"):
                router.call("ip/firewall/filter", "PUT", {
                    "chain": "input", "action": "accept", "protocol": protocol,
                    "dst-port": "53", "src-address": network,
                    "comment": "homelab-managed-router-dns-" + tag,
                    "place-before": "0",
                })

    for profile, password in wifi_updates:
        rest_set(router, f"interface/wifi/security/{rest_record_id(wifi_records[profile])}", {"passphrase": password})

    for identifier, network, gateway in networks:
        rows = router.call("ip/dhcp-server/network")
        match = next((row for row in rows if isinstance(row, dict) and row.get(".id") == identifier), None)
        expected = gateway if mode == "router" else dns_list
        if not match or match.get("dns-server", "").replace(" ", "") != expected.replace(" ", ""):
            raise RuntimeError(f"DHCP DNS verification failed for {network}; encrypted backup is available in Jenkins artifacts")
    dns_state = router.call("ip/dns")
    if isinstance(dns_state, list):
        dns_state = dns_state[0] if dns_state else {}
    expected_remote = "true" if mode == "router" else "false"
    if (dns_state.get("servers", "").replace(" ", "") != dns_list.replace(" ", "")
            or dns_state.get("allow-remote-requests") != expected_remote):
        raise RuntimeError("RouterOS DNS settings verification failed; encrypted backup is available in Jenkins artifacts")
    managed_rules = [
        rule for rule in router.call("ip/firewall/filter")
        if isinstance(rule, dict) and str(rule.get("comment", "")).startswith("homelab-managed-router-dns-")
    ]
    if mode == "router" and len(managed_rules) != 2 * len(networks):
        raise RuntimeError("RouterOS did not retain all managed DNS firewall rules; encrypted backup is available in Jenkins artifacts")
    if mode == "direct" and managed_rules:
        raise RuntimeError("Managed router DNS firewall rules remain in direct mode; encrypted backup is available in Jenkins artifacts")


def ssh_client(host, username, key_file, host_key_fields, password=None):
    client = paramiko.SSHClient()
    try:
        entry = HostKeyEntry.from_line(" ".join(host_key_fields))
        if entry is None:
            raise ValueError("invalid known_hosts entry")
        key_type, pinned_key = entry.key.get_name(), entry.key
    except Exception:
        raise RuntimeError("Could not load the pinned MikroTik SSH host key") from None
    client.get_host_keys().add(host, key_type, pinned_key)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        options = {
            "hostname": host,
            "username": username,
            "look_for_keys": False,
            "allow_agent": False,
            "timeout": 15,
            "banner_timeout": 15,
            "auth_timeout": 15,
        }
        if password is not None:
            options.update(password=password, pkey=None)
        else:
            options.update(key_filename=key_file, pkey=None)
        client.connect(**options)
    except Exception:
        client.close()
        raise RuntimeError("Could not authenticate to MikroTik using the pinned host key and configured credentials") from None
    return client


def apply(values):
    host = connection_host(values)
    host_key_host = values.get("MIKROTIK_HOST", "").strip()
    admin_user = values.get("MIKROTIK_BOOTSTRAP_USERNAME", "").strip()
    admin_password = values.get("MIKROTIK_BOOTSTRAP_PASSWORD", "")
    ssh_user = values.get("MIKROTIK_SSH_USER", "").strip()
    private_key = values.get("MIKROTIK_SSH_PRIVATE_KEY", "")
    public_key = values.get("MIKROTIK_SSH_PUBLIC_KEY", "").strip()
    trusted_host_key = values.get("MIKROTIK_SSH_HOST_KEY", "")
    backup_password = values.get("BINARY_BACKUP_PASSWORD", "")
    mode = values.get("MIKROTIK_DHCP_DNS_MODE", "").strip()

    if not re.fullmatch(r"[A-Za-z0-9.-]+", host_key_host) or host_key_host.startswith(".") or host_key_host.endswith("."):
        raise RuntimeError("Set a valid MIKROTIK_HOST in /mikrotik/router01")
    if admin_user and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", admin_user):
        raise RuntimeError("MIKROTIK_BOOTSTRAP_USERNAME must be a RouterOS username without spaces")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", ssh_user):
        raise RuntimeError("Set a valid MIKROTIK_SSH_USER in /mikrotik/router01")
    if not private_key.startswith("-----BEGIN OPENSSH PRIVATE KEY-----") or not public_key.startswith("ssh-ed25519 "):
        raise RuntimeError("Run Job 003 first to generate the MikroTik Ed25519 key pair")
    if not backup_password:
        raise RuntimeError("Set BINARY_BACKUP_PASSWORD in /mikrotik/backup before applying router changes")
    if mode not in ("router", "direct"):
        raise RuntimeError("Set MIKROTIK_DHCP_DNS_MODE to router or direct in /dns")

    dns_servers = []
    for item in (values.get("DNS01_IPV4", ""), values.get("DNS02_IPV4", "")):
        if not item:
            raise RuntimeError("Set DNS01_IPV4 and DNS02_IPV4 in /dns")
        try:
            dns_servers.append(str(ipaddress.IPv4Address(item.strip())))
        except ipaddress.AddressValueError:
            raise RuntimeError("DNS01_IPV4 and DNS02_IPV4 must be IPv4 addresses") from None
    fallbacks = [value.strip() for value in values.get("DNS_PUBLIC_FALLBACKS", "").split(",") if value.strip()]
    for fallback in fallbacks:
        try:
            ipaddress.ip_address(fallback)
        except ValueError:
            raise RuntimeError("DNS_PUBLIC_FALLBACKS must contain comma-separated IP addresses") from None
    upstreams = dns_servers + fallbacks
    host_fields = host_key_line(trusted_host_key, host_key_host)

    dns_only = os.environ.get("HOMELAB_MIKROTIK_DNS_ONLY", "").strip().lower() in ("1", "yes", "true")
    wifi = {} if dns_only else {
        "SEC_USERS_PASSWORD": "sec-users",
        "SEC_MGMT_PASSWORD": "sec-mgmt",
        "SEC_IOT_PASSWORD": "sec-iot",
        "SEC_GUEST_PASSWORD": "sec-guest",
    }
    wifi_updates = []
    for secret, profile in wifi.items():
        password = values.get(secret, "")
        if password:
            if len(password) < 8 or any(char in password for char in "\r\n\0"):
                raise RuntimeError(f"{secret} must be at least 8 characters and must not contain a line break")
            wifi_updates.append((profile, password))

    rest_user = values.get("MIKROTIK_USERNAME", "").strip()
    rest_password = values.get("MIKROTIK_PASSWORD", "")
    if not rest_user or not rest_password:
        rest_user = values.get("MIKROTIK_BOOTSTRAP_USERNAME", "").strip()
        rest_password = values.get("MIKROTIK_BOOTSTRAP_PASSWORD", "")
    rest = RouterREST(host, rest_user, rest_password, values.get("MIKROTIK_TLS_CERT_SHA256", ""))
    networks = rest_networks(rest)
    rest.call("system/resource")  # Authenticate before creating the backup.

    key_descriptor, key_filename = tempfile.mkstemp(prefix="mikrotik-ssh-key-")
    os.close(key_descriptor)
    key_path = Path(key_filename)
    key_path.write_text(private_key.rstrip("\n") + "\n", encoding="utf-8")
    os.chmod(key_path, 0o600)
    client = None
    admin_client = None
    try:
        # Keep SSH only for the encrypted binary backup and its file transfer.
        try:
            client = ssh_client(host, ssh_user, str(key_path), host_fields)
            key_login = True
        except RuntimeError:
            if not admin_user or not admin_password:
                raise RuntimeError("Stored MikroTik SSH key login failed. To bootstrap or repair it, set MIKROTIK_BOOTSTRAP_USERNAME and MIKROTIK_BOOTSTRAP_PASSWORD in /mikrotik/router01") from None
            admin_client = ssh_client(host, admin_user, str(key_path), host_fields, password=admin_password)
            create_backup(admin_client, backup_password)
            add_key = f"/user/ssh-keys/add user={ros_quote(ssh_user)} key={ros_quote(public_key)}"
            run_command(admin_client, add_key, allow_error=True)
            admin_client.close()
            admin_client = None
            client = ssh_client(host, ssh_user, str(key_path), host_fields)
            key_login = False
        if key_login:
            create_backup(client, backup_password)

        dns_list = ",".join(upstreams)
        rest_apply(rest, values, networks, mode, dns_list, wifi_updates)
        print(f"Configured MikroTik DNS upstreams in order: {', '.join(upstreams)}.")
        print(f"Set DHCP DNS for {len(networks)} network(s) using mode '{mode}'.")
        if wifi_updates:
            print(f"Updated Wi-Fi security profiles: {', '.join(profile for profile, _ in wifi_updates)}.")
        else:
            print("Wi-Fi settings were unchanged because no SEC_*_PASSWORD values were supplied.")
    finally:
        if client:
            client.close()
        if admin_client:
            admin_client.close()
        try:
            key_path.unlink()
        except FileNotFoundError:
            pass


def main():
    apply(infisical_secrets())


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
