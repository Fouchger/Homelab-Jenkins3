#!/usr/bin/env python3
"""Apply Infisical-managed DNS and Wi-Fi settings to RouterOS after an encrypted backup."""

import ipaddress
import json
import os
import re
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
            "DNS01_IPV4", "DNS02_IPV4", "DNS_PUBLIC_FALLBACKS", "MIKROTIK_DHCP_DNS_MODE",
        ),
        "/proxmox/mikrotik": (
            "MIKROTIK_HOST", "MIKROTIK_IP", "MIKROTIK_ADMIN_USER", "MIKROTIK_ADMIN_USER_PASSWORD",
            "MIKROTIK_SSH_USER", "MIKROTIK_SSH_PRIVATE_KEY", "MIKROTIK_SSH_PUBLIC_KEY",
            "MIKROTIK_SSH_HOST_KEY", "MIKROTIK_BACKUP_PASSWORD",
            "SEC_GUEST_PASSPHRASE", "SEC_IOT_PASSPHRASE", "SEC_MGMT_PASSPHRASE",
            "SEC_USERS_PASSPHRASE",
        ),
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
                values[name] = value
    return values


def ros_quote(value):
    if any(char in value for char in "\r\n\0"):
        raise RuntimeError("RouterOS setting contains an unsupported line break")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_export_networks(export_text):
    # RouterOS wraps long export commands with a trailing backslash.
    lines = []
    pending = ""
    for raw in export_text.splitlines():
        line = raw.strip()
        if pending:
            pending += " " + line
        else:
            pending = line
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        lines.append(pending)
        pending = ""
    if pending:
        lines.append(pending)

    section = ""
    networks = []
    for line in lines:
        if line.startswith("/"):
            if line.startswith("/ip dhcp-server network add "):
                section = "/ip dhcp-server network"
                line = line[len("/ip dhcp-server network "):]
            else:
                section = line
                continue
        if section != "/ip dhcp-server network" or not line.startswith("add "):
            continue
        fields = {}
        for match in re.finditer(r'([A-Za-z0-9-]+)=("(?:\\.|[^"\\])*"|[^\s]+)', line):
            value = match.group(2)
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1].replace('\\"', '"').replace('\\\\', '\\')
            fields[match.group(1)] = value
        address = fields.get("address", "")
        gateway = fields.get("gateway", "")
        if not address or not gateway:
            continue
        try:
            network = ipaddress.ip_network(address, strict=True)
            ipaddress.ip_address(gateway)
        except ValueError:
            continue
        networks.append((str(network), gateway))
    if not networks:
        raise RuntimeError("Could not read any DHCP network and gateway entries from the MikroTik export; no DNS changes were made")
    return networks


def host_key_line(value, host):
    lines = [line.strip() for line in value.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if len(lines) != 1:
        raise RuntimeError("MIKROTIK_SSH_HOST_KEY must contain one verified known_hosts entry")
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
        raise RuntimeError("Set a valid MIKROTIK_HOST in /proxmox/mikrotik")
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


def require_router_object(client, command, description):
    result = run_command(client, command).strip()
    if result != "1":
        raise RuntimeError(f"RouterOS must contain exactly one matching {description}; no router configuration was changed")


def verify_dhcp_dns(client, networks, mode, dns_servers):
    direct_dns = ",".join(dns_servers)
    for address, gateway in networks:
        require_router_object(
            client,
            f"/ip/dhcp-server/network/print count-only where address={ros_quote(address)}",
            f"DHCP network {address}",
        )
        configured_dns = run_command(
            client,
            f"/ip/dhcp-server/network/get [find where address={ros_quote(address)}] dns-server",
        ).strip().replace(" ", "")
        expected_dns = gateway if mode == "router" else direct_dns
        if configured_dns != expected_dns:
            raise RuntimeError(f"DHCP DNS verification failed for {address}; encrypted backup is available in Jenkins artifacts")


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
    admin_user = values.get("MIKROTIK_ADMIN_USER", "").strip()
    admin_password = values.get("MIKROTIK_ADMIN_USER_PASSWORD", "")
    ssh_user = values.get("MIKROTIK_SSH_USER", "").strip()
    private_key = values.get("MIKROTIK_SSH_PRIVATE_KEY", "")
    public_key = values.get("MIKROTIK_SSH_PUBLIC_KEY", "").strip()
    trusted_host_key = values.get("MIKROTIK_SSH_HOST_KEY", "")
    backup_password = values.get("MIKROTIK_BACKUP_PASSWORD", "")
    mode = values.get("MIKROTIK_DHCP_DNS_MODE", "").strip()

    if not re.fullmatch(r"[A-Za-z0-9.-]+", host_key_host) or host_key_host.startswith(".") or host_key_host.endswith("."):
        raise RuntimeError("Set a valid MIKROTIK_HOST in /proxmox/mikrotik")
    if admin_user and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", admin_user):
        raise RuntimeError("MIKROTIK_ADMIN_USER must be a RouterOS username without spaces")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", ssh_user):
        raise RuntimeError("Set a valid MIKROTIK_SSH_USER in /proxmox/mikrotik")
    if not private_key.startswith("-----BEGIN OPENSSH PRIVATE KEY-----") or not public_key.startswith("ssh-ed25519 "):
        raise RuntimeError("Run Job 003 first to generate the MikroTik Ed25519 key pair")
    if not backup_password:
        raise RuntimeError("Set MIKROTIK_BACKUP_PASSWORD in /proxmox/mikrotik before applying router changes")
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
        "SEC_USERS_PASSPHRASE": "sec-users",
        "SEC_MGMT_PASSPHRASE": "sec-mgmt",
        "SEC_IOT_PASSPHRASE": "sec-iot",
        "SEC_GUEST_PASSPHRASE": "sec-guest",
    }
    wifi_updates = []
    for secret, profile in wifi.items():
        password = values.get(secret, "")
        if password:
            if len(password) < 8 or any(char in password for char in "\r\n\0"):
                raise RuntimeError(f"{secret} must be at least 8 characters and must not contain a line break")
            wifi_updates.append((profile, password))

    key_descriptor, key_filename = tempfile.mkstemp(prefix="mikrotik-ssh-key-")
    os.close(key_descriptor)
    key_path = Path(key_filename)
    key_path.write_text(private_key.rstrip("\n") + "\n", encoding="utf-8")
    os.chmod(key_path, 0o600)
    client = None
    admin_client = None
    try:
        # Reuse an already-authorized key on subsequent runs. On first setup,
        # take the encrypted backup through the admin password session before
        # adding the SSH key, then switch to key authentication for changes.
        try:
            client = ssh_client(host, ssh_user, str(key_path), host_fields)
            key_login = True
        except RuntimeError:
            if not admin_user or not admin_password:
                raise RuntimeError("Stored MikroTik SSH key login failed. To bootstrap or repair it, set MIKROTIK_ADMIN_USER and MIKROTIK_ADMIN_USER_PASSWORD in /proxmox/mikrotik") from None
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

        export = run_command(client, "/export terse")
        networks = parse_export_networks(export)
        for address, _gateway in networks:
            require_router_object(
                client,
                f"/ip/dhcp-server/network/print count-only where address={ros_quote(address)}",
                f"DHCP network {address}",
            )
        for profile, _password in wifi_updates:
            require_router_object(
                client,
                f"/interface/wifi/security/print count-only where name={ros_quote(profile)}",
                f"Wi-Fi security profile {profile}",
            )

        dns_list = ",".join(upstreams)
        allow_remote = "yes" if mode == "router" else "no"
        run_command(client, f"/ip/dns/set allow-remote-requests={allow_remote} servers={ros_quote(dns_list)}")

        if mode == "router":
            for address, gateway in networks:
                dhcp_dns = gateway
                run_command(client, f"/ip/dhcp-server/network/set [find where address={ros_quote(address)}] dns-server={ros_quote(dhcp_dns)}")
            # Remove only this automation's prior rules, then permit DNS queries
            # from the current DHCP client subnets to the router itself.
            run_command(client, '/ip/firewall/filter/remove [find where comment~"homelab-managed-router-dns-"]', allow_error=True)
            for address, _gateway in networks:
                tag = re.sub(r"[^A-Za-z0-9.-]", "-", address)
                for protocol in ("udp", "tcp"):
                    run_command(
                        client,
                        f"/ip/firewall/filter/add chain=input action=accept protocol={protocol} dst-port=53 src-address={ros_quote(address)} comment={ros_quote('homelab-managed-router-dns-' + tag)} place-before=0",
                    )
        else:
            direct_dns = ",".join(dns_servers)
            for address, _gateway in networks:
                run_command(client, f"/ip/dhcp-server/network/set [find where address={ros_quote(address)}] dns-server={ros_quote(direct_dns)}")
            run_command(client, '/ip/firewall/filter/remove [find where comment~"homelab-managed-router-dns-"]', allow_error=True)

        for profile, password in wifi_updates:
            run_command(
                client,
                f"/interface/wifi/security/set [find where name={ros_quote(profile)}] passphrase={ros_quote(password)}",
            )

        verify_dhcp_dns(client, networks, mode, dns_servers)

        verified_dns = run_command(client, "/ip/dns/get servers").strip().replace(" ", "")
        if verified_dns != dns_list or run_command(client, "/ip/dns/get allow-remote-requests").strip() != allow_remote:
            raise RuntimeError("RouterOS DNS upstream verification did not match the requested Infisical values; encrypted backup is available in Jenkins artifacts")
        print(f"Configured MikroTik DNS upstreams in order: {', '.join(upstreams)}.")
        print(f"Set DHCP DNS for {len(networks)} network(s) using mode '{mode}'.")
        if wifi_updates:
            print(f"Updated Wi-Fi security profiles: {', '.join(profile for profile, _ in wifi_updates)}.")
        else:
            print("Wi-Fi passphrases were unchanged because no SEC_*_PASSPHRASE values were supplied.")
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
