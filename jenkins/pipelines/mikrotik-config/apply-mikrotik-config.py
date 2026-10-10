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
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
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
            "MIKROTIK_USERNAME", "MIKROTIK_PASSWORD", "MIKROTIK_TLS_CERT_SHA256",
            "MIKROTIK_SSH_USER", "MIKROTIK_SSH_PRIVATE_KEY", "MIKROTIK_SSH_PUBLIC_KEY", "MIKROTIK_SSH_HOST_KEY",
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
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$") + '"'


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

    def call(self, path, method="GET", payload=None):
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
            if response.status >= 400:
                detail = ""
                try:
                    error_body = json.loads(raw.decode("utf-8")) if raw else {}
                    if isinstance(error_body, dict):
                        message = error_body.get("detail") or error_body.get("message")
                        if isinstance(message, str):
                            detail = " ".join(message.split())[:240]
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
                suffix = f"; RouterOS says: {detail}" if detail else ""
                raise RuntimeError(f"RouterOS REST {method} {path} failed with HTTP {response.status}{suffix}")
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
    """Return a validated RouterOS REST record ID for use in a resource path."""
    identifier = record.get(".id") if isinstance(record, dict) else None
    if not isinstance(identifier, str) or not re.fullmatch(r"\*[A-Za-z0-9]+", identifier):
        raise RuntimeError("RouterOS REST record did not include a valid .id")
    return urllib.parse.quote(identifier, safe="*")


def normalize_ssh_fingerprint(value):
    fingerprint = str(value or "").strip().rstrip("=")
    return fingerprint if fingerprint.startswith("SHA256:") else "SHA256:" + fingerprint


def ensure_ssh_key(router, username, public_key):
    fields = public_key.strip().split()
    if len(fields) < 3 or fields[0] != "ssh-ed25519":
        raise RuntimeError("MIKROTIK_SSH_PUBLIC_KEY must be the generated Ed25519 public key from Job 003")
    try:
        key_blob = base64.b64decode(fields[1], validate=True)
    except (ValueError, base64.binascii.Error):
        raise RuntimeError("MIKROTIK_SSH_PUBLIC_KEY is not a valid OpenSSH public key") from None
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(key_blob).digest()).decode("ascii").rstrip("=")
    comment = " ".join(fields[2:])
    users = router.call("user?.proplist=name,group,disabled")
    if not isinstance(users, list):
        raise RuntimeError("RouterOS returned an invalid user list while checking the SSH backup account")
    account = next((row for row in users if isinstance(row, dict) and row.get("name") == username), None)
    if not account:
        raise RuntimeError("MIKROTIK_SSH_USER does not exist on RouterOS; create a dedicated backup user before running Job 004")
    if account.get("disabled") == "true":
        raise RuntimeError("MIKROTIK_SSH_USER is disabled on RouterOS")
    group_name = account.get("group", "")
    groups = router.call("user/group?.proplist=.id,name,policy")
    group = next((row for row in groups if isinstance(row, dict) and row.get("name") == group_name), None) if isinstance(groups, list) else None
    policy_items = [item.strip() for item in str(group.get("policy", "")).split(",") if item.strip()] if group else []
    policies = {item.lower() for item in policy_items if not item.startswith("!")}
    missing_policies = {"read", "write", "ftp", "sensitive"} - policies
    if missing_policies:
        raise RuntimeError(
            f"MIKROTIK_SSH_USER group '{group_name}' is missing required backup policies: "
            + ", ".join(sorted(missing_policies))
        )
    if "ssh" not in policies:
        members = [row for row in users if isinstance(row, dict) and row.get("group") == group_name]
        if len(members) != 1 or members[0].get("name") != username:
            raise RuntimeError(f"MIKROTIK_SSH_USER group '{group_name}' does not allow SSH login and is shared by other RouterOS users; assign a dedicated group before running Job 004")
        if not group:
            raise RuntimeError(f"Could not read RouterOS group '{group_name}' to safely enable SSH login")
        # This is an approved, reversible change. Only modify a group that is
        # used exclusively by the dedicated Jenkins backup user.
        updated_policy = [item for item in policy_items if item.lower() not in ("ssh", "!ssh")]
        updated_policy.append("ssh")
        router.call(f"user/group/{rest_record_id(group)}", "PATCH", {"policy": ",".join(updated_policy)})
        groups = router.call("user/group?.proplist=.id,name,policy")
        group = next((row for row in groups if isinstance(row, dict) and row.get("name") == group_name), None) if isinstance(groups, list) else None
        policies = {item.strip().lower() for item in str(group.get("policy", "")).split(",")
                    if item.strip() and not item.strip().startswith("!")} if group else set()
        if "ssh" not in policies:
            raise RuntimeError(f"RouterOS did not confirm the ssh policy for dedicated group '{group_name}'")
        print(f"Enabled SSH login for the dedicated MikroTik backup group '{group_name}'.")

    path = "user/ssh-keys?.proplist=.id,user,info,key-type,bits,fingerprint"
    rows = router.call(path)
    if not isinstance(rows, list):
        raise RuntimeError("RouterOS returned an invalid SSH key list")
    matches = [row for row in rows if isinstance(row, dict)
               and row.get("user") == username and row.get("info") == comment
               and row.get("key-type") == "ed25519" and row.get("bits") == "256"
               and normalize_ssh_fingerprint(row.get("fingerprint")) == fingerprint]
    if matches:
        return
    try:
        result = router.call("user/ssh-keys", "PUT", {"user": username, "key": public_key.strip()})
    except RuntimeError as exc:
        detail = str(exc)
        if "not enough permissions" in detail.lower():
            raise RuntimeError("RouterOS denied Jenkins SSH key authorization for lack of permissions. The REST account needs the RouterOS policy required to manage SSH keys (usually policy and write).") from None
        raise RuntimeError(f"RouterOS rejected Jenkins SSH key authorization: {detail}") from None
    if isinstance(result, dict) and result.get("error"):
        raise RuntimeError("RouterOS rejected Jenkins SSH key authorization; check REST account user-management permission")
    rows = router.call(path)
    if not isinstance(rows, list) or not any(
        isinstance(row, dict) and row.get("user") == username and row.get("info") == comment
        and row.get("key-type") == "ed25519" and row.get("bits") == "256"
        and normalize_ssh_fingerprint(row.get("fingerprint")) == fingerprint
        for row in rows
    ):
        raise RuntimeError("RouterOS did not confirm the exact Jenkins SSH key fingerprint for the backup user")


def ssh_host_key(value, host):
    lines = [line.strip() for line in value.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if len(lines) != 1:
        raise RuntimeError("MIKROTIK_SSH_HOST_KEY must contain one pinned known_hosts entry")
    fields = lines[0].split()
    aliases = fields[0].split(",") if fields else []
    if len(fields) < 3 or host not in aliases or not fields[1].startswith("ssh-"):
        raise RuntimeError("MIKROTIK_SSH_HOST_KEY does not match MIKROTIK_HOST")
    entry = HostKeyEntry.from_line(" ".join(fields))
    if entry is None:
        raise RuntimeError("Could not parse the pinned MikroTik SSH host key")
    return fields[1], entry.key


def connect_ssh(host, username, host_key, private_key):
    key_type, pinned_key = host_key
    client = paramiko.SSHClient()
    client.get_host_keys().add(host, key_type, pinned_key)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    key_path = None
    try:
        descriptor, key_name = tempfile.mkstemp(prefix="homelab-mikrotik-ssh-")
        key_path = Path(key_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as key_file:
            key_file.write(private_key.rstrip("\n") + "\n")
        client.connect(
            hostname=host, username=username, key_filename=str(key_path),
            look_for_keys=False, allow_agent=False, timeout=15,
            banner_timeout=15, auth_timeout=15,
        )
        return client
    except paramiko.AuthenticationException:
        client.close()
        raise RuntimeError("RouterOS rejected the Jenkins SSH key; verify the key fingerprint is authorized for MIKROTIK_SSH_USER and the user's group allows SSH login") from None
    except paramiko.BadHostKeyException:
        client.close()
        raise RuntimeError("RouterOS SSH host key does not match the saved MIKROTIK_SSH_HOST_KEY pin") from None
    except Exception:
        client.close()
        raise RuntimeError("Could not connect to MikroTik over SSH; check port 22 reachability and RouterOS SSH service settings") from None
    finally:
        if key_path:
            try:
                key_path.unlink()
            except FileNotFoundError:
                pass


def ssh_command(client, command_text):
    try:
        _stdin, stdout, stderr = client.exec_command(command_text, timeout=60)
        output = stdout.read()
        error = stderr.read()
        status = stdout.channel.recv_exit_status()
    except Exception:
        raise RuntimeError("RouterOS SSH command failed; command output was suppressed") from None
    combined = (output + b"\n" + error).decode("utf-8", errors="replace")
    if status != 0 or re.search(r"(?im)^\s*(failure|error|script error):|not enough permissions", combined):
        if "not enough permissions" in combined.lower():
            raise RuntimeError("RouterOS denied the backup command. The SSH backup account needs ssh, read, write, sensitive, and ftp permissions.")
        raise RuntimeError("RouterOS rejected a backup command; command output was suppressed")
    return combined


def fetch_router_file(client, remote_name, local_path):
    sftp = client.open_sftp()
    try:
        try:
            sftp.get(remote_name, str(local_path))
        except OSError:
            sftp.get("/" + remote_name, str(local_path))
    except Exception:
        raise RuntimeError("RouterOS created the pre-change backup but Jenkins could not download it over SFTP") from None
    finally:
        sftp.close()
    if not local_path.is_file() or local_path.stat().st_size == 0:
        raise RuntimeError("RouterOS returned an empty pre-change backup file")


def remove_router_files(client, names):
    sftp = client.open_sftp()
    try:
        existing_files = set(sftp.listdir("."))
    finally:
        sftp.close()
    failed = False
    for name in names:
        if name in existing_files:
            try:
                ssh_command(client, f"/file/remove [find where name={ros_quote(name)}]")
            except RuntimeError:
                failed = True
    if failed:
        raise RuntimeError("RouterOS could not remove one or more temporary backup files")


def cleanup_orphaned_router_backups(client):
    sftp = client.open_sftp()
    try:
        existing_files = sftp.listdir(".")
    finally:
        sftp.close()
    pattern = re.compile(r"homelab-router-before-dns-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}\.(?:backup|rsc)\Z")
    stale = [name for name in existing_files if pattern.fullmatch(name)]
    if stale:
        remove_router_files(client, stale)
        print(f"Removed {len(stale)} abandoned pre-change backup file(s) from earlier runs.")


def create_encrypted_backup(client, backup_password):
    """Pull encrypted binary and sensitive text backups over the pinned SSH connection."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base_name = f"homelab-router-before-dns-{stamp}-{uuid.uuid4().hex[:8]}"
    build_number = os.environ.get("BUILD_NUMBER", "").strip()
    if not re.fullmatch(r"[0-9]+", build_number):
        raise RuntimeError("Jenkins BUILD_NUMBER is missing or invalid; cannot safely archive this run's backup")
    artifact_dir = Path("artifacts") / build_number
    artifact_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(artifact_dir, 0o700)
    artifact_path = artifact_dir / (base_name + ".tar.enc")
    temporary_artifact = artifact_dir / (base_name + ".tar.enc.tmp")
    remote_files = (base_name + ".backup", base_name + ".rsc")
    backup_complete = False
    try:
        cleanup_orphaned_router_backups(client)
        ssh_command(client, f"/system/backup/save name={ros_quote(base_name)} password={ros_quote(backup_password)}")
        ssh_command(client, f"/export show-sensitive file={ros_quote(base_name)}")
        with tempfile.TemporaryDirectory(prefix="homelab-router-backup-") as temporary_dir:
            private_dir = Path(temporary_dir)
            os.chmod(private_dir, 0o700)
            local_files = [private_dir / name for name in remote_files]
            for remote_name, local_path in zip(remote_files, local_files):
                fetch_router_file(client, remote_name, local_path)
                os.chmod(local_path, 0o600)
            plaintext_archive = private_dir / (base_name + ".tar")
            with tarfile.open(plaintext_archive, "w") as archive:
                for local_path in local_files:
                    archive.add(local_path, arcname=local_path.name)
            os.chmod(plaintext_archive, 0o600)
            openssl = shutil.which("openssl")
            if not openssl:
                raise RuntimeError("OpenSSL is required on the Jenkins agent to encrypt the pre-change backup")
            encrypted = subprocess.run(
                [openssl, "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "600000", "-salt",
                 "-in", str(plaintext_archive), "-out", str(temporary_artifact), "-pass", "stdin"],
                input=(backup_password + "\n").encode("utf-8"),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180, check=False,
            )
            if encrypted.returncode != 0 or not temporary_artifact.is_file() or temporary_artifact.stat().st_size < 32:
                raise RuntimeError("Could not encrypt the RouterOS pre-change backup; no router settings were changed")
            with temporary_artifact.open("rb") as result:
                if result.read(8) != b"Salted__":
                    raise RuntimeError("OpenSSL produced an unexpected encrypted backup format")
            os.chmod(temporary_artifact, 0o600)
            os.replace(temporary_artifact, artifact_path)
            backup_complete = True
        print(f"Encrypted pre-change RouterOS text and binary backups saved as Jenkins artifact {artifact_path.name}.")
    except subprocess.TimeoutExpired:
        raise RuntimeError("Encrypting the RouterOS pre-change backup timed out; no router settings were changed") from None
    finally:
        operation_failed = sys.exc_info()[0] is not None
        cleanup_failed = False
        try:
            remove_router_files(client, remote_files)
        except Exception:
            cleanup_failed = True
        try:
            temporary_artifact.unlink()
        except FileNotFoundError:
            pass
        if cleanup_failed:
            if operation_failed:
                print("WARNING: Could not confirm removal of temporary RouterOS backup files; the original backup error is reported above.")
            elif backup_complete:
                raise RuntimeError(
                    "Could not remove temporary sensitive backup files from RouterOS; "
                    "no router settings were applied and the encrypted backup artifact was retained"
                ) from None
            else:
                raise RuntimeError(
                    "Could not confirm removal of temporary RouterOS backup files; no router settings were applied"
                ) from None


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
        networks.append((
            rest_record_id(row), network, gateway,
            str(row.get("dns-server", "")),
        ))
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

    expected_remote = "true" if mode == "router" else "false"
    dns_changed = (
        str(dns_state.get("servers", "")).replace(" ", "") != dns_list.replace(" ", "")
        or dns_state.get("allow-remote-requests") != expected_remote
    )
    if dns_changed:
        router.call("ip/dns/set", "POST", {
            "allow-remote-requests": expected_remote,
            "servers": dns_list,
        })

    dhcp_updates = []
    expected_dhcp_dns = {}
    for identifier, network, gateway, current_dns in networks:
        desired = gateway if mode == "router" else dns_list
        expected_dhcp_dns[identifier] = (network, desired)
        if current_dns.replace(" ", "") != desired.replace(" ", ""):
            rest_set(router, f"ip/dhcp-server/network/{identifier}", {"dns-server": desired})
            dhcp_updates.append(network)

    managed_prefix = "homelab-managed-router-dns-"
    desired_rules = {}
    for _identifier, network, _gateway, _current_dns in networks:
        if mode == "router":
            tag = re.sub(r"[^A-Za-z0-9.-]", "-", network)
            comment = managed_prefix + tag
            for protocol in ("udp", "tcp"):
                desired_rules[(comment, protocol)] = {
                    "chain": "input", "action": "accept", "protocol": protocol,
                    "dst-port": "53", "src-address": network,
                    "comment": comment,
                }

    kept_rules = set()
    firewall_removed = 0
    for rule in existing_rules:
        if not isinstance(rule, dict) or not str(rule.get("comment", "")).startswith(managed_prefix):
            continue
        key = (str(rule.get("comment", "")), str(rule.get("protocol", "")))
        expected = desired_rules.get(key)
        valid = bool(expected) and all(str(rule.get(field, "")) == value for field, value in expected.items())
        valid = valid and str(rule.get("disabled", "false")).lower() != "true"
        if valid and key not in kept_rules:
            kept_rules.add(key)
        else:
            router.call(f"ip/firewall/filter/{rest_record_id(rule)}", "DELETE")
            firewall_removed += 1

    firewall_added = 0
    for key, payload in desired_rules.items():
        if key not in kept_rules:
            router.call("ip/firewall/filter", "PUT", {**payload, "place-before": "0"})
            firewall_added += 1

    for profile, password in wifi_updates:
        rest_set(router, f"interface/wifi/security/{rest_record_id(wifi_records[profile])}", {"passphrase": password})

    for identifier, (network, expected) in expected_dhcp_dns.items():
        rows = router.call("ip/dhcp-server/network")
        match = next((row for row in rows if isinstance(row, dict) and row.get(".id") == identifier), None)
        if not match or match.get("dns-server", "").replace(" ", "") != expected.replace(" ", ""):
            raise RuntimeError(f"DHCP DNS verification failed for {network}; encrypted backup is available in Jenkins artifacts")
    dns_state = router.call("ip/dns")
    if isinstance(dns_state, list):
        dns_state = dns_state[0] if dns_state else {}
    if (dns_state.get("servers", "").replace(" ", "") != dns_list.replace(" ", "")
            or dns_state.get("allow-remote-requests") != expected_remote):
        raise RuntimeError("RouterOS DNS settings verification failed; encrypted backup is available in Jenkins artifacts")
    managed_rules = [
        rule for rule in router.call("ip/firewall/filter")
        if isinstance(rule, dict) and str(rule.get("comment", "")).startswith("homelab-managed-router-dns-")
    ]
    actual_rules = {
        (str(rule.get("comment", "")), str(rule.get("protocol", "")))
        for rule in managed_rules if isinstance(rule, dict)
    }
    if actual_rules != set(desired_rules) or len(managed_rules) != len(desired_rules):
        raise RuntimeError("RouterOS did not retain all managed DNS firewall rules; encrypted backup is available in Jenkins artifacts")
    return {
        "dns_changed": dns_changed,
        "dhcp_updates": dhcp_updates,
        "firewall_added": firewall_added,
        "firewall_removed": firewall_removed,
        "wifi_updated": [profile for profile, _password in wifi_updates],
    }


def apply(values):
    host = connection_host(values)
    backup_password = values.get("BINARY_BACKUP_PASSWORD", "")
    mode = values.get("MIKROTIK_DHCP_DNS_MODE", "").strip()

    if len(backup_password) < 8 or any(char in backup_password for char in "\r\n\0"):
        raise RuntimeError("Set BINARY_BACKUP_PASSWORD in /mikrotik/backup to at least 8 characters without line breaks")
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
    bootstrap_user = values.get("MIKROTIK_BOOTSTRAP_USERNAME", "").strip()
    bootstrap_password = values.get("MIKROTIK_BOOTSTRAP_PASSWORD", "")
    if not rest_user or not rest_password:
        rest_user = bootstrap_user
        rest_password = bootstrap_password
    rest = RouterREST(host, rest_user, rest_password, values.get("MIKROTIK_TLS_CERT_SHA256", ""))
    networks = rest_networks(rest)
    rest.call("system/resource")  # Authenticate before creating the backup.
    ssh_user = values.get("MIKROTIK_SSH_USER", "").strip()
    private_key = values.get("MIKROTIK_SSH_PRIVATE_KEY", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", ssh_user):
        raise RuntimeError("Set a valid MIKROTIK_SSH_USER in /mikrotik/router01")
    if not private_key.startswith("-----BEGIN OPENSSH PRIVATE KEY-----"):
        raise RuntimeError("Run Job 003 first to generate MIKROTIK_SSH_PRIVATE_KEY")
    pinned_ssh_key = ssh_host_key(values.get("MIKROTIK_SSH_HOST_KEY", ""), host)
    # Key authorization changes a RouterOS user's SSH credentials and needs
    # user-management rights. Keep routine settings on the limited REST
    # account, and use the stored bootstrap administrator only for this step.
    key_authorizer = rest
    if bootstrap_user and bootstrap_password and (bootstrap_user != rest_user or bootstrap_password != rest_password):
        key_authorizer = RouterREST(
            host, bootstrap_user, bootstrap_password,
            values.get("MIKROTIK_TLS_CERT_SHA256", ""),
        )
    ensure_ssh_key(key_authorizer, ssh_user, values.get("MIKROTIK_SSH_PUBLIC_KEY", ""))
    ssh = connect_ssh(host, ssh_user, pinned_ssh_key, private_key)
    try:
        create_encrypted_backup(ssh, backup_password)
    finally:
        ssh.close()
    dns_list = ",".join(upstreams)
    changes = rest_apply(rest, networks, mode, dns_list, wifi_updates)
    if changes["dns_changed"]:
        print(f"Configured MikroTik DNS upstreams in order: {', '.join(upstreams)}.")
    else:
        print(f"MikroTik DNS upstreams already match: {', '.join(upstreams)}.")
    if changes["dhcp_updates"]:
        print(f"Updated DHCP DNS for {len(changes['dhcp_updates'])} network(s) using mode '{mode}': {', '.join(changes['dhcp_updates'])}.")
    else:
        print(f"DHCP DNS already matches mode '{mode}' for all {len(networks)} network(s).")
    if changes["wifi_updated"]:
        print(f"Updated Wi-Fi security profiles: {', '.join(changes['wifi_updated'])}.")
    else:
        print("Wi-Fi settings were unchanged because no SEC_*_PASSWORD values were supplied.")
    if changes["firewall_added"] or changes["firewall_removed"]:
        print(f"Managed DNS firewall rules changed: added {changes['firewall_added']}, removed {changes['firewall_removed']}.")
    else:
        print("Managed DNS firewall rules already match the selected mode.")


def main():
    apply(infisical_secrets())


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
