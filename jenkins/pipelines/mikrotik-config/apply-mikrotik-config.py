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
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path



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
            "MIKROTIK_USERNAME", "MIKROTIK_PASSWORD", "MIKROTIK_TLS_CERT_SHA256",
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


def connection_host(values):
    """Use the management endpoint normally; only reset recovery overrides it."""
    host = os.environ.get("HOMELAB_MIKROTIK_CONNECT_HOST", "").strip()
    if not host:
        host = values.get("MIKROTIK_HOST", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host) or host.startswith(".") or host.endswith("."):
        raise RuntimeError("Set a valid MIKROTIK_HOST in /mikrotik/router01")
    return host


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


def create_encrypted_export(router, backup_password):
    """Create and archive a sensitive RouterOS export without opening SSH/SFTP."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    export_name = f"homelab-router-before-dns-{stamp}.rsc"
    build_number = os.environ.get("BUILD_NUMBER", "").strip()
    if not re.fullmatch(r"[0-9]+", build_number):
        raise RuntimeError("Jenkins BUILD_NUMBER is missing or invalid; cannot safely archive this run's backup")
    artifact_dir = Path("artifacts") / build_number
    artifact_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(artifact_dir, 0o700)
    artifact_path = artifact_dir / (export_name + ".enc")
    temporary_artifact = artifact_dir / (export_name + ".enc.tmp")
    plaintext_path = None
    export_attempted = False
    try:
        cleanup_orphaned_sensitive_exports(router)
        # The request can time out after RouterOS has already created the file.
        # Always try deleting this unique name, even when the request errors.
        export_attempted = True
        created = router.call("export", "POST", {"show-sensitive": "", "file": export_name})
        if isinstance(created, dict) and created.get("error"):
            raise RuntimeError("RouterOS could not create the pre-change export; no router settings were changed")
        files = router.call("file?.proplist=.id,name")
        matches = [item for item in files if isinstance(item, dict) and item.get("name") == export_name]
        if len(matches) != 1:
            raise RuntimeError("RouterOS did not create exactly one pre-change export; no router settings were changed")
        rest_record_id(matches[0])  # Validate the returned record before reading it.

        chunks = []
        offset = 0
        max_export_size = 32 * 1024 * 1024
        while True:
            response = router.call("file/read", "POST", {
                "file": export_name, "offset": str(offset), "chunk-size": "32768",
            })
            if isinstance(response, dict):
                response = [response]
            if not isinstance(response, list) or len(response) != 1 or not isinstance(response[0], dict):
                raise RuntimeError("RouterOS returned an invalid pre-change export chunk")
            chunk = response[0].get("data", "")
            if not isinstance(chunk, str):
                raise RuntimeError("RouterOS returned invalid export data")
            if not chunk:
                break
            encoded = chunk.encode("utf-8")
            chunks.append(encoded)
            offset += len(encoded)
            if offset > max_export_size:
                raise RuntimeError("RouterOS pre-change export exceeded the 32 MiB safety limit")
            if len(encoded) < 32768:
                break

        export_bytes = b"".join(chunks)
        if not export_bytes or b"/" not in export_bytes:
            raise RuntimeError("RouterOS pre-change export was empty or malformed")
        openssl = shutil.which("openssl")
        if not openssl:
            raise RuntimeError("OpenSSL is required on the Jenkins agent to encrypt the pre-change export")
        descriptor, plaintext_name = tempfile.mkstemp(prefix="homelab-router-export-", suffix=".rsc")
        plaintext_path = Path(plaintext_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as plaintext:
            plaintext.write(export_bytes)
        del export_bytes, chunks

        encrypted = subprocess.run(
            [openssl, "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "600000", "-salt",
             "-in", str(plaintext_path), "-out", str(temporary_artifact), "-pass", "stdin"],
            input=(backup_password + "\n").encode("utf-8"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180, check=False,
        )
        if encrypted.returncode != 0 or not temporary_artifact.is_file() or temporary_artifact.stat().st_size < 32:
            raise RuntimeError("Could not encrypt the RouterOS pre-change export; no router settings were changed")
        with temporary_artifact.open("rb") as result:
            if result.read(8) != b"Salted__":
                raise RuntimeError("OpenSSL produced an unexpected encrypted export format")
        os.chmod(temporary_artifact, 0o600)
        os.replace(temporary_artifact, artifact_path)
        print(f"Encrypted pre-change RouterOS sensitive configuration export saved as Jenkins artifact {artifact_path.name}.")
    except subprocess.TimeoutExpired:
        raise RuntimeError("Encrypting the RouterOS pre-change export timed out; no router settings were changed") from None
    finally:
        if plaintext_path:
            try:
                plaintext_path.unlink()
            except FileNotFoundError:
                pass
        try:
            temporary_artifact.unlink()
        except FileNotFoundError:
            pass
        if export_attempted:
            try:
                delete_sensitive_export(router, export_name)
            except RuntimeError as exc:
                raise RuntimeError(f"Could not remove the temporary sensitive export from the MikroTik: {exc}") from None


def rest_record_id(record):
    identifier = record.get(".id") if isinstance(record, dict) else None
    if not isinstance(identifier, str) or not re.fullmatch(r"\*[0-9A-Fa-f]+", identifier):
        raise RuntimeError("RouterOS REST returned an invalid record ID")
    return identifier


def delete_router_file(router, record):
    """RouterOS REST deletes a file resource by its returned .id, not its name."""
    identifier = rest_record_id(record)
    router.call(f"file/{identifier}", "DELETE", allow_404=True)


def delete_sensitive_export(router, name):
    records = router.call("file?.proplist=.id,name")
    if not isinstance(records, list):
        raise RuntimeError("RouterOS returned an invalid file list during temporary export cleanup")
    matches = [record for record in records if isinstance(record, dict) and record.get("name") == name]
    if len(matches) > 1:
        raise RuntimeError("RouterOS returned duplicate temporary export names; refusing ambiguous cleanup")
    if matches:
        delete_router_file(router, matches[0])


def cleanup_orphaned_sensitive_exports(router):
    """Remove only leftover sensitive exports created by this automation."""
    records = router.call("file?.proplist=.id,name")
    if not isinstance(records, list):
        raise RuntimeError("RouterOS returned an invalid file list before creating the backup")
    pattern = re.compile(r"(?:flash/)?homelab-router-before-dns-[0-9]{8}T[0-9]{6}Z\.rsc\Z")
    stale = [record for record in records if isinstance(record, dict)
             and isinstance(record.get("name"), str) and pattern.fullmatch(record["name"])]
    for record in stale:
        delete_router_file(router, record)
    if stale:
        print(f"Removed {len(stale)} abandoned temporary sensitive export(s) from earlier runs.")


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


def rest_apply(router, networks, mode, dns_list, wifi_updates):
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


def apply(values):
    host = connection_host(values)
    backup_password = values.get("BINARY_BACKUP_PASSWORD", "")
    mode = values.get("MIKROTIK_DHCP_DNS_MODE", "").strip()

    if not backup_password:
        raise RuntimeError("Set BINARY_BACKUP_PASSWORD in /mikrotik/backup to encrypt the pre-change RouterOS export")
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
    create_encrypted_export(rest, backup_password)
    dns_list = ",".join(upstreams)
    rest_apply(rest, networks, mode, dns_list, wifi_updates)
    print(f"Configured MikroTik DNS upstreams in order: {', '.join(upstreams)}.")
    print(f"Set DHCP DNS for {len(networks)} network(s) using mode '{mode}'.")
    if wifi_updates:
        print(f"Updated Wi-Fi security profiles: {', '.join(profile for profile, _ in wifi_updates)}.")
    else:
        print("Wi-Fi settings were unchanged because no SEC_*_PASSWORD values were supplied.")


def main():
    apply(infisical_secrets())


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
