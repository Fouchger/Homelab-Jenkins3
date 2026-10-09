#!/usr/bin/env python3
"""Validate operator-supplied network settings and persist them to Infisical."""

import ipaddress
import hashlib
import hmac
import base64
import http.client
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid


CONFIG = {
    "/proxmox/lxc/dns01": {"LXC_ROOT_PASSWORD": "CFG_DNS01_ROOT_PASSWORD"},
    "/proxmox/lxc/dns02": {"LXC_ROOT_PASSWORD": "CFG_DNS02_ROOT_PASSWORD"},
    "/dns": {
        "MIKROTIK_DHCP_DNS_MODE": "CFG_MIKROTIK_DHCP_DNS_MODE",
        "DNS_PUBLIC_FALLBACKS": "CFG_DNS_PUBLIC_FALLBACKS",
        "DNS_HOSTED_ZONES": "CFG_DNS_HOSTED_ZONES",
    },
    "/dns/dns01": {"DNS_IPV4": "CFG_DNS01_IPV4", "DNS_SERVER_ADMIN_PASSWORD": "CFG_DNS01_ADMIN_PASSWORD"},
    "/dns/dns02": {"DNS_IPV4": "CFG_DNS02_IPV4", "DNS_SERVER_ADMIN_PASSWORD": "CFG_DNS02_ADMIN_PASSWORD"},
    "/mikrotik/router01": {
        "MIKROTIK_HOST": "CFG_MIKROTIK_HOST",
        "MIKROTIK_IP": "CFG_MIKROTIK_IP",
        "MIKROTIK_USERNAME": "CFG_MIKROTIK_USERNAME",
        "MIKROTIK_PASSWORD": "CFG_MIKROTIK_PASSWORD",
        "MIKROTIK_BOOTSTRAP_USERNAME": "CFG_MIKROTIK_BOOTSTRAP_USERNAME",
        "MIKROTIK_BOOTSTRAP_PASSWORD": "CFG_MIKROTIK_BOOTSTRAP_PASSWORD",
        "MIKROTIK_SSH_USER": "CFG_MIKROTIK_SSH_USER",
        "MIKROTIK_SSH_PRIVATE_KEY": "CFG_MIKROTIK_SSH_PRIVATE_KEY",
        "MIKROTIK_SSH_PUBLIC_KEY": "CFG_MIKROTIK_SSH_PUBLIC_KEY",
        "MIKROTIK_SSH_HOST_KEY": "CFG_MIKROTIK_SSH_HOST_KEY",
        "MIKROTIK_TLS_CERT_SHA256": "CFG_MIKROTIK_TLS_CERT_SHA256",
    },
    "/mikrotik/backup": {"BINARY_BACKUP_PASSWORD": "CFG_BINARY_BACKUP_PASSWORD"},
    "/mikrotik/wifi_security": {
        "SEC_GUEST_PASSWORD": "CFG_SEC_GUEST_PASSWORD",
        "SEC_IOT_PASSWORD": "CFG_SEC_IOT_PASSWORD",
        "SEC_MGMT_PASSWORD": "CFG_SEC_MGMT_PASSWORD",
        "SEC_USERS_PASSWORD": "CFG_SEC_USERS_PASSWORD",
    },
    "/cloudflare": {
        "CLOUDFLARE_ACCOUNT_ID": "CFG_CLOUDFLARE_ACCOUNT_ID",
        "CLOUDFLARE_API_TOKEN": "CFG_CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_DOMAIN_1": "CFG_CLOUDFLARE_DOMAIN_1",
        "CLOUDFLARE_DOMAIN_2": "CFG_CLOUDFLARE_DOMAIN_2",
        "CLOUDFLARE_ZONE_ID_1": "CFG_CLOUDFLARE_ZONE_ID_1",
        "CLOUDFLARE_ZONE_ID_2": "CFG_CLOUDFLARE_ZONE_ID_2",
    },
    "/dockflare": {
        "DOCKFLARE_ACCESS_EMAILS": "CFG_DOCKFLARE_ACCESS_EMAILS",
        "DOCKFLARE_ADMIN_CIDRS": "CFG_DOCKFLARE_ADMIN_CIDRS",
    },
}


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required Jenkins/Infisical setting {name} is missing")
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
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if allow_404 and exc.code == 404:
            return None
        raise RuntimeError(f"Infisical request failed with HTTP {exc.code}; response was suppressed") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Infisical request failed: {type(exc).__name__}") from None
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("Infisical returned an invalid JSON response") from None


def infisical_login(base_url):
    response = request_json(
        f"{base_url}/api/v1/auth/universal-auth/login",
        method="POST",
        form={
            "clientId": required("INFISICAL_WRITE_CLIENT_ID"),
            "clientSecret": required("INFISICAL_WRITE_CLIENT_SECRET"),
        },
    )
    token = response.get("accessToken") if isinstance(response, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError("Infisical write identity did not authenticate")
    return token


def secret_url(base_url, secret_path, name):
    query = urllib.parse.urlencode({
        "projectId": required("INFISICAL_PROJECT_ID"),
        "environment": required("INFISICAL_ENVIRONMENT"),
        "secretPath": secret_path,
    })
    return f"{base_url}/api/v4/secrets/{urllib.parse.quote(name)}?{query}"


def read_secret(base_url, token, secret_path, name):
    result = request_json(secret_url(base_url, secret_path, name), token=token, allow_404=True)
    if not result:
        return None
    secret = result.get("secret", {}) if isinstance(result, dict) else {}
    value = secret.get("secretValue") if isinstance(secret, dict) else None
    return value if isinstance(value, str) else None


def write_secret(base_url, token, secret_path, name, value, existed):
    url = secret_url(base_url, secret_path, name)
    body = {
        "projectId": required("INFISICAL_PROJECT_ID"),
        "environment": required("INFISICAL_ENVIRONMENT"),
        "secretValue": value,
        "secretPath": secret_path,
        "type": "shared",
        "skipMultilineEncoding": True,
    }
    request_json(url, method="PATCH" if existed else "POST", token=token, body=body)


def delete_secret(base_url, token, secret_path, name):
    request_json(secret_url(base_url, secret_path, name), method="DELETE", token=token, allow_404=True)


def validate_domain(value, name):
    value = value.lower().rstrip(".")
    if len(value) > 253 or not re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", value):
        raise RuntimeError(f"{name} must be a public DNS domain name")
    return value


def validate_email_list(value):
    emails = [item.strip() for item in value.split(",") if item.strip()]
    pattern = re.compile(r"^[^\s@,]+@[^\s@,.]+(?:\.[^\s@,.]+)+$")
    if any(not pattern.fullmatch(email) for email in emails):
        raise RuntimeError("DOCKFLARE_ACCESS_EMAILS must be a comma-separated list of email addresses")
    return ",".join(emails)


def validate_cidr_list(value):
    networks = [item.strip() for item in value.split(",") if item.strip()]
    try:
        return ",".join(str(ipaddress.ip_network(item, strict=False)) for item in networks)
    except ValueError:
        raise RuntimeError("DOCKFLARE_ADMIN_CIDRS must be a comma-separated list of IP networks") from None


def validate_value(name, value):
    if name in ("MIKROTIK_HOST",):
        if not re.fullmatch(r"(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|(?:\d{1,3}\.){3}\d{1,3})", value):
            raise RuntimeError("MIKROTIK_HOST must be a hostname or IPv4 address without a scheme or port")
        if value.count(".") == 3 and all(part.isdecimal() for part in value.split(".")):
            try:
                return str(ipaddress.IPv4Address(value))
            except ipaddress.AddressValueError:
                raise RuntimeError("MIKROTIK_HOST is not a valid IPv4 address") from None
        return value
    if name in ("MIKROTIK_USERNAME", "MIKROTIK_BOOTSTRAP_USERNAME", "MIKROTIK_SSH_USER"):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
            raise RuntimeError(f"{name} must be a RouterOS username without spaces")
        return value
    if name in ("MIKROTIK_PASSWORD", "MIKROTIK_BOOTSTRAP_PASSWORD", "BINARY_BACKUP_PASSWORD"):
        if len(value) < 8 or any(char in value for char in "\r\n\0"):
            raise RuntimeError(f"{name} must be at least 8 characters and must not contain a line break")
        return value
    if name.endswith("_PASSWORD") and name.startswith("SEC_"):
        if len(value) < 8 or any(char in value for char in "\r\n\0"):
            raise RuntimeError(f"{name} must be at least 8 characters and must not contain a line break")
        return value
    if name == "DNS_IPV4":
        try:
            return str(ipaddress.IPv4Address(value))
        except ipaddress.AddressValueError:
            raise RuntimeError(f"{name} must be an IPv4 address") from None
    if name == "MIKROTIK_IP":
        try:
            return str(ipaddress.IPv4Address(value))
        except ipaddress.AddressValueError:
            raise RuntimeError("MIKROTIK_IP must be an IPv4 address used after a full router reset") from None
    if name == "DNS_PUBLIC_FALLBACKS":
        addresses = [item.strip() for item in value.split(",") if item.strip()]
        try:
            return ",".join(str(ipaddress.ip_address(item)) for item in addresses)
        except ValueError:
            raise RuntimeError("DNS_PUBLIC_FALLBACKS must be a comma-separated list of IP addresses") from None
    if name == "MIKROTIK_DHCP_DNS_MODE":
        mode = " ".join(value.strip().lower().replace("_", " ").replace("/", " ").split())
        if mode in ("router", "mikrotik", "mikrotik router", "mikrotik resolver", "router mikrotik resolver"):
            return "router"
        if mode in ("direct", "technitium", "technitium direct", "direct technitium"):
            return "direct"
        raise RuntimeError("MIKROTIK_DHCP_DNS_MODE must be router (MikroTik resolver) or direct (Technitium)")
    if name == "CLOUDFLARE_ACCOUNT_ID":
        if not re.fullmatch(r"[0-9a-fA-F]{32}", value):
            raise RuntimeError("CLOUDFLARE_ACCOUNT_ID must be a 32-character hexadecimal account ID")
        return value.lower()
    if name in ("CLOUDFLARE_DOMAIN_1", "CLOUDFLARE_DOMAIN_2"):
        return validate_domain(value, name)
    if name in ("CLOUDFLARE_ZONE_ID_1", "CLOUDFLARE_ZONE_ID_2"):
        if not re.fullmatch(r"[0-9a-fA-F]{32}", value):
            raise RuntimeError(f"{name} must be a 32-character hexadecimal Zone ID")
        return value.lower()
    if name == "DOCKFLARE_ACCESS_EMAILS":
        return validate_email_list(value)
    if name == "DOCKFLARE_ADMIN_CIDRS":
        return validate_cidr_list(value)
    if name == "CLOUDFLARE_API_TOKEN":
        if any(char in value for char in "\r\n\0"):
            raise RuntimeError("CLOUDFLARE_API_TOKEN must be a single-line value")
        return value
    if name == "MIKROTIK_TLS_CERT_SHA256":
        normalized = value.lower().replace(":", "")
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise RuntimeError("MIKROTIK_TLS_CERT_SHA256 must be a 64-character SHA-256 fingerprint (colons are optional)")
        return normalized
    if name == "LXC_ROOT_PASSWORD":
        if len(value) < 6 or ":" in value or any(char in value for char in "\r\n\0"):
            raise RuntimeError("LXC root passwords must be at least 6 characters and cannot contain a colon or line break")
        return value
    if name == "DNS_SERVER_ADMIN_PASSWORD":
        if len(value) < 6 or any(char in value for char in "\r\n\0"):
            raise RuntimeError(f"{name} must be at least 6 characters and must not contain a line break")
        return value
    if name == "DNS_HOSTED_ZONES":
        domains = [validate_domain(item.strip(), name) for item in value.split(",") if item.strip()]
        if len(domains) != len(set(domains)):
            raise RuntimeError("DNS_HOSTED_ZONES must not contain duplicate domains")
        return ",".join(domains)
    raise RuntimeError("Unsupported configuration item")


def router_rest_request(host, tls_fingerprint, username, password, method, path, body=None):
    """Send one REST request only after checking the saved HTTPS certificate pin."""
    connection = http.client.HTTPSConnection(host, 443, timeout=20, context=ssl._create_unverified_context())
    headers = {
        "Authorization": "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode("ascii"),
        "Accept": "application/json",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    try:
        connection.connect()
        peer = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
        if not hmac.compare_digest(peer, tls_fingerprint):
            raise RuntimeError("Router HTTPS certificate does not match the saved trust pin")
        connection.request(method, "/rest/" + path, body=data, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        if response.status >= 400:
            raise RuntimeError(f"RouterOS SSH host-key verification request failed with HTTP {response.status}; response was suppressed")
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError("RouterOS returned invalid JSON while verifying its SSH host key") from None
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        raise RuntimeError(f"Router HTTPS request failed while verifying its SSH host key ({type(exc).__name__})") from None
    finally:
        connection.close()


def router_host_key_fingerprints(host, tls_fingerprint, username, password):
    """Read the router's public SSH key over pinned HTTPS, then remove temporary exports."""
    if not re.fullmatch(r"[0-9a-f]{64}", tls_fingerprint):
        raise RuntimeError("Set and verify MIKROTIK_TLS_CERT_SHA256 before automatically verifying the SSH host key")
    if not username or not password:
        raise RuntimeError("Set a RouterOS REST username and password before automatically verifying the SSH host key")
    prefix = "jenkins_hostkey_" + uuid.uuid4().hex
    files = []
    try:
        router_rest_request(
            host, tls_fingerprint, username, password, "POST", "execute",
            {"script": f"/ip/ssh/export-host-key key-file-prefix={prefix}", "as-string": ""},
        )
        records = router_rest_request(
            host, tls_fingerprint, username, password, "GET",
            "file?.proplist=.id,name",
        )
        if not isinstance(records, list):
            raise RuntimeError("RouterOS did not return its temporary SSH key export files")
        files = [item for item in records if isinstance(item, dict) and str(item.get("name", "")).startswith(prefix)]
        public_keys = [item for item in files if str(item.get("name", "")).endswith("_pub.pem")]
        fingerprints = []
        if not public_keys:
            raise RuntimeError("RouterOS did not provide an exported SSH public host key")
        for item in public_keys:
            file_id = item.get(".id")
            if not file_id:
                raise RuntimeError("RouterOS did not identify its exported SSH public host key")
            content_record = router_rest_request(
                host, tls_fingerprint, username, password, "GET",
                "file/" + urllib.parse.quote(str(file_id), safe="*") + "?.proplist=contents",
            )
            contents = content_record.get("contents") if isinstance(content_record, dict) else None
            if not isinstance(contents, str) or "BEGIN PUBLIC KEY" not in contents:
                raise RuntimeError("RouterOS returned an unreadable SSH public host key")
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="mikrotik-router-host-", suffix=".pem", delete=True) as key_file:
                key_file.write(contents)
                key_file.flush()
                converted = subprocess.run(
                    ["ssh-keygen", "-i", "-m", "PKCS8", "-f", key_file.name],
                    check=True, capture_output=True, text=True, timeout=20,
                ).stdout.strip()
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="mikrotik-router-host-", suffix=".pub", delete=True) as key_file:
                key_file.write(converted + "\n")
                key_file.flush()
                fingerprints.append(subprocess.run(
                    ["ssh-keygen", "-lf", key_file.name, "-E", "sha256"],
                    check=True, capture_output=True, text=True, timeout=20,
                ).stdout.split()[1])
        return fingerprints
    except (OSError, subprocess.SubprocessError, IndexError) as exc:
        raise RuntimeError(f"Could not convert the RouterOS public SSH key ({type(exc).__name__})") from None
    finally:
        cleanup_error = False
        if not files:
            try:
                records = router_rest_request(host, tls_fingerprint, username, password, "GET", "file?.proplist=.id,name")
                files = [item for item in records if isinstance(item, dict) and str(item.get("name", "")).startswith(prefix)] if isinstance(records, list) else []
            except Exception:
                cleanup_error = True
        for item in files:
            file_id = item.get(".id")
            if file_id:
                try:
                    router_rest_request(host, tls_fingerprint, username, password, "DELETE", "file/" + urllib.parse.quote(str(file_id), safe="*"))
                except Exception:
                    cleanup_error = True
        if cleanup_error:
            raise RuntimeError("Could not remove temporary SSH key export files from RouterOS; check WinBox Files and delete jenkins_hostkey_* files")


def router_key_material(existing_private, existing_public, existing_host_key, host, tls_fingerprint, username, password):
    """Create missing client credentials and pin the SSH key to the HTTPS-authenticated router."""
    host = host.strip()
    private_key = existing_private
    public_key = existing_public
    host_key = existing_host_key
    generated = {}

    if not private_key:
        if not host:
            return generated
        with tempfile.TemporaryDirectory(prefix="mikrotik-ssh-") as directory:
            key_path = os.path.join(directory, "id_ed25519")
            try:
                subprocess.run(
                    ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"homelab-jenkins-{host}", "-f", key_path],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
                )
                private_key = open(key_path, encoding="utf-8").read().rstrip("\n")
                public_key = open(key_path + ".pub", encoding="utf-8").read().strip()
            except (OSError, subprocess.SubprocessError):
                raise RuntimeError("Could not generate the MikroTik Ed25519 SSH key; confirm openssh-client is installed on the Jenkins agent") from None
        generated["MIKROTIK_SSH_PRIVATE_KEY"] = private_key + "\n"
        generated["MIKROTIK_SSH_PUBLIC_KEY"] = public_key

    if private_key and not public_key:
        with tempfile.TemporaryDirectory(prefix="mikrotik-ssh-") as directory:
            key_path = os.path.join(directory, "id_ed25519")
            try:
                with open(key_path, "w", encoding="utf-8") as output:
                    output.write(private_key.rstrip("\n") + "\n")
                os.chmod(key_path, 0o600)
                result = subprocess.run(["ssh-keygen", "-y", "-f", key_path], check=True, capture_output=True, text=True, timeout=30)
                public_key = result.stdout.strip()
            except (OSError, subprocess.SubprocessError):
                raise RuntimeError("Could not derive the public key from the stored MikroTik private key") from None
        generated["MIKROTIK_SSH_PUBLIC_KEY"] = public_key

    if not host_key and host:
        try:
            expected_fingerprints = router_host_key_fingerprints(host, tls_fingerprint, username, password)
            scanned = subprocess.run(
                ["ssh-keyscan", "-T", "10", "-t", "ed25519,rsa,ecdsa", host],
                check=False, capture_output=True, text=True, timeout=20,
            )
            candidates = [line for line in scanned.stdout.splitlines() if line.strip() and not line.startswith("#")]
            if scanned.returncode or not candidates:
                raise RuntimeError("ssh-keyscan returned no usable RouterOS host key")
            known_host_line = ""
            fingerprints = []
            for candidate in candidates:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="mikrotik-host-key-", delete=True) as key_file:
                    key_file.write(candidate.strip() + "\n")
                    key_file.flush()
                    fingerprint = subprocess.run(
                        ["ssh-keygen", "-lf", key_file.name, "-E", "sha256"],
                        check=True, capture_output=True, text=True, timeout=20,
                    ).stdout.split()[1]
                fingerprints.append(fingerprint)
                if fingerprint in expected_fingerprints:
                    known_host_line = candidate.strip()
                    break
            if not known_host_line:
                raise RuntimeError("SSH host key scanned by Jenkins did not match the public key returned over pinned HTTPS; no host key was saved")
            host_key = known_host_line
            generated["MIKROTIK_SSH_HOST_KEY"] = host_key
        except RuntimeError:
            raise
        except (OSError, subprocess.SubprocessError, IndexError):
            raise RuntimeError("Could not verify the MikroTik SSH host key against the key returned over pinned HTTPS") from None
    return generated


def router_tls_fingerprint(host):
    """Read the presented certificate fingerprint; never treat scanning as trust."""
    try:
        with socket.create_connection((host, 443), timeout=15) as raw:
            with ssl._create_unverified_context().wrap_socket(raw, server_hostname=host) as tls:
                return hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
    except (OSError, ssl.SSLError):
        raise RuntimeError("Could not read the MikroTik HTTPS certificate on port 443") from None


def verify_router_tls_pin(host, expected):
    """Ensure the entered fingerprint matches the certificate served at MIKROTIK_HOST."""
    if not expected:
        return
    actual = router_tls_fingerprint(host)
    if not hmac.compare_digest(actual, expected):
        raise RuntimeError("The entered MikroTik HTTPS fingerprint does not match the certificate currently served by MIKROTIK_HOST; it was not saved")


def save_one_secret(base_url, token, secret_path, name, value):
    """Write and verify one value; restore only this value if verification fails."""
    previous = read_secret(base_url, token, secret_path, name)
    try:
        write_secret(base_url, token, secret_path, name, value, previous is not None)
        if read_secret(base_url, token, secret_path, name) != value:
            raise RuntimeError("Infisical read-back did not match the submitted value")
    except Exception:
        try:
            if previous is None:
                delete_secret(base_url, token, secret_path, name)
            else:
                write_secret(base_url, token, secret_path, name, previous, True)
        except Exception:
            raise RuntimeError("save failed and rollback could not be verified") from None
        raise RuntimeError("save failed; previous value was restored") from None


def main():
    base_url = required("INFISICAL_URL").rstrip("/")
    if not base_url.startswith("https://"):
        raise RuntimeError("INFISICAL_URL must use HTTPS")
    updates = {}
    results = {}
    for secret_path, items in CONFIG.items():
        for name, environment_name in items.items():
            # Jenkins exposes build parameters as environment variables. Read
            # those directly instead of interpolating password parameters into
            # a Groovy withEnv list.
            parameter_name = environment_name.removeprefix("CFG_")
            value = os.environ.get(parameter_name, "").strip()
            if value:
                key = (secret_path, name)
                try:
                    updates[key] = validate_value(name, value)
                except RuntimeError as exc:
                    results[key] = ("FAILED", str(exc))
    token = infisical_login(base_url)
    router_path = "/mikrotik/router01"
    try:
        stored_router_host = read_secret(base_url, token, router_path, "MIKROTIK_HOST") or ""
    except Exception as exc:
        stored_router_host = ""
        results[(router_path, "MIKROTIK_HOST")] = ("FAILED", f"could not read current router host: {exc}")
    router_host = updates.get((router_path, "MIKROTIK_HOST"), stored_router_host)
    if router_host and not any(path == router_path for path, _ in results):
        changed = bool(stored_router_host and router_host != stored_router_host)
        if not os.environ.get("MIKROTIK_HOST", "").strip():
            os.environ["MIKROTIK_HOST"] = router_host
        try:
            private = read_secret(base_url, token, router_path, "MIKROTIK_SSH_PRIVATE_KEY") or ""
            public = read_secret(base_url, token, router_path, "MIKROTIK_SSH_PUBLIC_KEY") or ""
            host_key = read_secret(base_url, token, router_path, "MIKROTIK_SSH_HOST_KEY") or ""
            if changed:
                host_key = ""
            tls_pin = updates.get((router_path, "MIKROTIK_TLS_CERT_SHA256"), "")
            stored_tls = read_secret(base_url, token, router_path, "MIKROTIK_TLS_CERT_SHA256") or ""
            effective_tls_pin = tls_pin or stored_tls
            if tls_pin:
                verify_router_tls_pin(router_host, tls_pin)
            elif changed and not stored_tls:
                observed = router_tls_fingerprint(router_host)
                print("RouterOS HTTPS certificate fingerprint seen by this Jenkins agent: " + observed)
                raise RuntimeError("MIKROTIK_HOST changed; verify and enter MIKROTIK_TLS_CERT_SHA256 before SSH host-key verification")
            elif changed:
                verify_router_tls_pin(router_host, stored_tls)
            elif not stored_tls:
                observed = router_tls_fingerprint(router_host)
                print("RouterOS HTTPS certificate fingerprint seen by this Jenkins agent: " + observed)
                print("Verify it through a trusted local connection and enter MIKROTIK_TLS_CERT_SHA256 on the next run; this scanned value was not saved.")
            rest_username = updates.get((router_path, "MIKROTIK_USERNAME")) or read_secret(base_url, token, router_path, "MIKROTIK_USERNAME") or ""
            rest_password = updates.get((router_path, "MIKROTIK_PASSWORD")) or read_secret(base_url, token, router_path, "MIKROTIK_PASSWORD") or ""
            if not rest_username or not rest_password:
                rest_username = updates.get((router_path, "MIKROTIK_BOOTSTRAP_USERNAME")) or read_secret(base_url, token, router_path, "MIKROTIK_BOOTSTRAP_USERNAME") or ""
                rest_password = updates.get((router_path, "MIKROTIK_BOOTSTRAP_PASSWORD")) or read_secret(base_url, token, router_path, "MIKROTIK_BOOTSTRAP_PASSWORD") or ""
            generated = router_key_material(
                private, public, host_key, router_host, effective_tls_pin,
                rest_username, rest_password,
            )
            if "MIKROTIK_SSH_HOST_KEY" in generated:
                print("RouterOS SSH host key verified against its public key over the pinned HTTPS connection.")
            for name, value in generated.items():
                updates[(router_path, name)] = value
        except Exception as exc:
            for key in list(updates):
                if key[0] == router_path:
                    results[key] = ("FAILED", str(exc))
                    updates.pop(key)

    for key, value in updates.items():
        path, name = key
        try:
            save_one_secret(base_url, token, path, name, value)
            results[key] = ("SAVED", "")
        except Exception as exc:
            results[key] = ("FAILED", str(exc))

    print("Settings results (blank inputs were left unchanged):")
    for secret_path, items in CONFIG.items():
        for name in items:
            key = (secret_path, name)
            if key in results:
                status, detail = results[key]
                print(f"{status}: {secret_path}/{name}" + (f" — {detail}" if detail else ""))
    if not updates and not results:
        print("No settings changed. Existing Infisical values are ready for Review.")
    public_key = read_secret(base_url, token, "/mikrotik/router01", "MIKROTIK_SSH_PUBLIC_KEY")
    if public_key:
        print("Generated Jenkins SSH public key (public key only; Job 004 authorizes it with the stored RouterOS administrator login):")
        print(public_key)
    print("No router, DNS server, or Cloudflare service configuration was applied by this settings job.")
    if any(status == "FAILED" for status, _ in results.values()):
        raise RuntimeError("One or more settings failed; review the per-variable results above and correct only those entries.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
