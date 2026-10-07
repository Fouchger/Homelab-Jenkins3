#!/usr/bin/env python3
"""Validate operator-supplied network settings and persist them to Infisical."""

import ipaddress
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request


CONFIG = {
    "/dns": {
        "DNS_PUBLIC_FALLBACKS": "CFG_DNS_PUBLIC_FALLBACKS",
        "MIKROTIK_DHCP_DNS_MODE": "CFG_MIKROTIK_DHCP_DNS_MODE",
        "DNS_HOSTED_ZONES": "CFG_DNS_HOSTED_ZONES",
    },
    "/dns/dns01": {"DNS_IPV4": "CFG_DNS01_IPV4", "DNS_SERVER_ADMIN_PASSWORD": "CFG_DNS01_ADMIN_PASSWORD"},
    "/dns/dns02": {"DNS_IPV4": "CFG_DNS02_IPV4", "DNS_SERVER_ADMIN_PASSWORD": "CFG_DNS02_ADMIN_PASSWORD"},
    "/proxmox/lxc/dns01": {"LXC_ROOT_PASSWORD": "CFG_DNS01_ROOT_PASSWORD"},
    "/proxmox/lxc/dns02": {"LXC_ROOT_PASSWORD": "CFG_DNS02_ROOT_PASSWORD"},
    "/mikrotik/router01": {
        "MIKROTIK_HOST": "CFG_MIKROTIK_HOST",
        "MIKROTIK_IP": "CFG_MIKROTIK_IP",
        "MIKROTIK_USERNAME": "CFG_MIKROTIK_USERNAME",
        "MIKROTIK_PASSWORD": "CFG_MIKROTIK_PASSWORD",
        "MIKROTIK_BOOTSTRAP_USERNAME": "CFG_MIKROTIK_BOOTSTRAP_USERNAME",
        "MIKROTIK_BOOTSTRAP_PASSWORD": "CFG_MIKROTIK_BOOTSTRAP_PASSWORD",
        "MIKROTIK_SSH_USER": "CFG_MIKROTIK_SSH_USER",
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
        if value not in ("router", "direct"):
            raise RuntimeError("MIKROTIK_DHCP_DNS_MODE must be router or direct")
        return value
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


def router_key_material(existing_private, existing_public, existing_host_key):
    """Create missing client credentials, pinning a scanned host key to an operator-supplied fingerprint."""
    host = os.environ.get("CFG_MIKROTIK_HOST", "").strip()
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
        expected = os.environ.get("CFG_MIKROTIK_SSH_HOST_KEY_FINGERPRINT", "").strip()
        try:
            scanned = subprocess.run(
                ["ssh-keyscan", "-T", "10", "-t", "ed25519,rsa,ecdsa", host],
                check=False, capture_output=True, text=True, timeout=20,
            )
            candidates = [line for line in scanned.stdout.splitlines() if line.strip() and not line.startswith("#")]
            if scanned.returncode or not candidates:
                if not expected:
                    print("RouterOS SSH host key could not be scanned yet. The client key pair and other settings will still be saved; rerun this job after SSH is reachable to pin the host key.")
                    return generated
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
                if fingerprint == expected:
                    known_host_line = candidate.strip()
                    break
            if not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", expected):
                print("RouterOS SSH fingerprint(s) seen by this Jenkins agent: " + ", ".join(fingerprints))
                print("Compare a fingerprint with the router's trusted value from a separate source, then rerun with it in MIKROTIK_SSH_HOST_KEY_FINGERPRINT. The client key pair and other settings will still be saved; the host key is not trusted yet.")
                return generated
            if not known_host_line:
                raise RuntimeError("The router's scanned SSH fingerprint does not match the independently verified fingerprint; no host key was saved")
            host_key = known_host_line
            generated["MIKROTIK_SSH_HOST_KEY"] = host_key
        except RuntimeError:
            raise
        except (OSError, subprocess.SubprocessError, IndexError):
            raise RuntimeError("Could not verify the MikroTik SSH host key; no host key was saved") from None
    return generated


def main():
    base_url = required("INFISICAL_URL").rstrip("/")
    if not base_url.startswith("https://"):
        raise RuntimeError("INFISICAL_URL must use HTTPS")
    updates = {}
    for secret_path, items in CONFIG.items():
        for name, environment_name in items.items():
            value = os.environ.get(environment_name, "").strip()
            if value:
                updates[(secret_path, name)] = validate_value(name, value)
    token = infisical_login(base_url)
    stored_router_host = read_secret(base_url, token, "/mikrotik/router01", "MIKROTIK_HOST") or ""
    router_host = updates.get(("/mikrotik/router01", "MIKROTIK_HOST"), stored_router_host)
    if router_host:
        router_host_changed = bool(stored_router_host and router_host != stored_router_host)
        supplied_fingerprint = os.environ.get("CFG_MIKROTIK_SSH_HOST_KEY_FINGERPRINT", "").strip()
        if router_host_changed and not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", supplied_fingerprint):
            raise RuntimeError("Changing MIKROTIK_HOST requires its independently verified SHA256 fingerprint; the existing router host and key were left unchanged")
        if not os.environ.get("CFG_MIKROTIK_HOST", "").strip():
            os.environ["CFG_MIKROTIK_HOST"] = router_host
        existing_router_key = read_secret(base_url, token, "/mikrotik/router01", "MIKROTIK_SSH_PRIVATE_KEY") or ""
        existing_router_pub = read_secret(base_url, token, "/mikrotik/router01", "MIKROTIK_SSH_PUBLIC_KEY") or ""
        existing_router_host_key = read_secret(base_url, token, "/mikrotik/router01", "MIKROTIK_SSH_HOST_KEY") or ""
        if router_host_changed:
            existing_router_host_key = ""
        generated = router_key_material(existing_router_key, existing_router_pub, existing_router_host_key)
        for name, value in generated.items():
            updates[("/mikrotik/router01", name)] = value

    if not updates:
        raise RuntimeError("No settings were supplied and no missing MikroTik SSH key material needed generation")

    previous = {
        key: read_secret(base_url, token, key[0], key[1])
        for key in updates
    }
    try:
        for (secret_path, name), value in updates.items():
            write_secret(base_url, token, secret_path, name, value, previous[(secret_path, name)] is not None)
        for (secret_path, name), value in updates.items():
            if read_secret(base_url, token, secret_path, name) != value:
                raise RuntimeError(f"Infisical could not verify {secret_path}/{name}")
    except Exception:
        failures = []
        for (secret_path, name), old_value in previous.items():
            try:
                if old_value is None:
                    delete_secret(base_url, token, secret_path, name)
                else:
                    write_secret(base_url, token, secret_path, name, old_value, True)
            except Exception:
                failures.append(f"{secret_path}/{name}")
        if failures:
            raise RuntimeError("Infisical save failed and rollback was incomplete; inspect the affected settings in Infisical") from None
        raise RuntimeError("Infisical save or verification failed; the previous setting values were restored") from None

    for secret_path in CONFIG:
        names = sorted(name for path, name in updates if path == secret_path)
        if names:
            print(f"Saved and verified {', '.join(names)} in {secret_path}.")
    public_key = read_secret(base_url, token, "/mikrotik/router01", "MIKROTIK_SSH_PUBLIC_KEY")
    if public_key:
        print("Generated Jenkins SSH public key (public key only; Job 004 authorizes it with the stored RouterOS administrator login):")
        print(public_key)
    print("Blank inputs were left unchanged. No router, DNS server, or Cloudflare service configuration was applied by this settings job.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
