#!/usr/bin/env python3
"""Back up, reset, and restore RouterOS using the protected Infisical script."""

import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import secrets
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import paramiko
from paramiko.hostkeys import HostKeyEntry


def required(values, name):
    value = values.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required Infisical value {name} is missing")
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


def infisical_url(secret_path, name):
    query = urllib.parse.urlencode({
        "projectId": os.environ.get("INFISICAL_PROJECT_ID", "").strip(),
        "environment": os.environ.get("INFISICAL_ENVIRONMENT", "").strip(),
        "secretPath": secret_path,
    })
    base_url = os.environ.get("INFISICAL_URL", "").strip().rstrip("/")
    return f"{base_url}/api/v4/secrets/{urllib.parse.quote(name)}?{query}"


def save_verified_host_key(value, existed=True):
    """Persist the host key only after matching SSH against the pinned HTTPS key."""
    base_url = os.environ.get("INFISICAL_URL", "").strip().rstrip("/")
    login = api_json(
        f"{base_url}/api/v1/auth/universal-auth/login", method="POST",
        form={
            "clientId": os.environ.get("INFISICAL_WRITE_CLIENT_ID", ""),
            "clientSecret": os.environ.get("INFISICAL_WRITE_CLIENT_SECRET", ""),
        },
    )
    token = login.get("accessToken") if isinstance(login, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError("Infisical write identity did not authenticate to save the verified post-reset SSH host key")
    url = infisical_url("/mikrotik/router01", "MIKROTIK_SSH_HOST_KEY")
    body = json.dumps({
        "projectId": os.environ.get("INFISICAL_PROJECT_ID", "").strip(),
        "environment": os.environ.get("INFISICAL_ENVIRONMENT", "").strip(),
        "secretPath": "/mikrotik/router01",
        "secretValue": value,
        "type": "shared",
        "skipMultilineEncoding": True,
    }).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json",
        "Content-Type": "application/json",
    }, method="PATCH" if existed else "POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Infisical could not save the verified post-reset SSH host key (HTTP {exc.code})") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Infisical could not save the verified post-reset SSH host key ({type(exc).__name__})") from None
    read_login = api_json(
        f"{base_url}/api/v1/auth/universal-auth/login", method="POST",
        form={
            "clientId": os.environ.get("INFISICAL_READ_CLIENT_ID", ""),
            "clientSecret": os.environ.get("INFISICAL_READ_CLIENT_SECRET", ""),
        },
    )
    read_token = read_login.get("accessToken") if isinstance(read_login, dict) else None
    if not isinstance(read_token, str) or not read_token:
        raise RuntimeError("Infisical read identity did not authenticate to confirm the saved post-reset SSH host key")
    saved = api_json(url, token=read_token)
    saved_secret = saved.get("secret", {}) if isinstance(saved, dict) else {}
    if not isinstance(saved_secret, dict) or saved_secret.get("secretValue") != value:
        raise RuntimeError("Infisical did not confirm the verified post-reset SSH host key")


def router_rest_request(host, tls_fingerprint, username, password, method, path, body=None):
    """Call RouterOS REST only when the presented TLS certificate matches its saved pin."""
    connection = http.client.HTTPSConnection(host, 443, timeout=12, context=ssl._create_unverified_context())
    headers = {
        "Authorization": "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode("ascii"),
        "Accept": "application/json",
    }
    data = json.dumps(body).encode("utf-8") if body is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    try:
        connection.connect()
        peer = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
        if not hmac.compare_digest(peer, tls_fingerprint):
            raise RuntimeError("RouterOS HTTPS certificate does not match the saved TLS pin")
        connection.request(method, "/rest/" + path, body=data, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        if response.status >= 400:
            raise RuntimeError(f"RouterOS HTTPS REST request failed with HTTP {response.status}")
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError("RouterOS returned invalid JSON during post-reset SSH host-key verification") from None
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        raise RuntimeError(f"RouterOS HTTPS is not ready at the recovery address ({type(exc).__name__})") from None
    finally:
        connection.close()


def router_ssh_host_key(host, tls_fingerprint, username, password, canonical_host, recovery_alias=""):
    """Match the recovery address SSH scan with the public key from pinned REST."""
    prefix = "jenkins_hostkey_" + secrets.token_hex(16)
    files = []
    export_completed = False
    cleanup_error = False
    try:
        exported = router_rest_request(host, tls_fingerprint, username, password, "POST", "execute", {
            "script": f"/ip/ssh/export-host-key key-file-prefix={prefix}", "as-string": "",
        })
        export_completed = True
        for _ in range(8):
            records = router_rest_request(host, tls_fingerprint, username, password, "GET", "file?.proplist=.id,name")
            if not isinstance(records, list):
                raise RuntimeError("RouterOS returned an invalid file list while verifying its SSH host key")
            files = [item for item in records if isinstance(item, dict) and str(item.get("name", "")).startswith(prefix)]
            if files:
                break
            time.sleep(1)
        public_files = [item for item in files if str(item.get("name", "")).endswith("_pub.pem")]
        if not public_files:
            output = exported.get("ret", "") if isinstance(exported, dict) else ""
            if re.search(r"permission|policy|not enough rights", str(output), re.IGNORECASE):
                raise RuntimeError("RouterOS REST account lacks the sensitive policy needed to export the SSH host key")
            raise RuntimeError("RouterOS did not provide its post-reset SSH public host key")
        public_fingerprints = set()
        for item in public_files:
            file_id = item.get(".id")
            if not file_id:
                raise RuntimeError("RouterOS did not identify its post-reset SSH public host-key file")
            record = router_rest_request(
                host, tls_fingerprint, username, password, "GET",
                "file/" + urllib.parse.quote(str(file_id), safe="*") + "?.proplist=contents",
            )
            contents = record.get("contents") if isinstance(record, dict) else None
            if not isinstance(contents, str) or "BEGIN PUBLIC KEY" not in contents:
                raise RuntimeError("RouterOS returned an unreadable post-reset SSH public host key")
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".pem", delete=True) as key_file:
                key_file.write(contents)
                key_file.flush()
                converted = subprocess.run(
                    ["ssh-keygen", "-i", "-m", "PKCS8", "-f", key_file.name],
                    check=True, capture_output=True, text=True, timeout=20,
                ).stdout.strip()
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".pub", delete=True) as key_file:
                key_file.write(converted + "\n")
                key_file.flush()
                public_fingerprints.add(subprocess.run(
                    ["ssh-keygen", "-lf", key_file.name, "-E", "sha256"],
                    check=True, capture_output=True, text=True, timeout=20,
                ).stdout.split()[1])

        scanned = subprocess.run(
            ["ssh-keyscan", "-T", "8", "-t", "ed25519,rsa,ecdsa", host],
            check=False, capture_output=True, text=True, timeout=20,
        )
        if not any(line.strip() and not line.lstrip().startswith("#") for line in scanned.stdout.splitlines()):
            raise RuntimeError("RouterOS SSH service is not ready at the recovery address")
        for candidate in scanned.stdout.splitlines():
            fields = candidate.strip().split(None, 2)
            if len(fields) < 3 or fields[0].startswith("#"):
                continue
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".pub", delete=True) as key_file:
                key_file.write(candidate.strip() + "\n")
                key_file.flush()
                fingerprint = subprocess.run(
                    ["ssh-keygen", "-lf", key_file.name, "-E", "sha256"],
                    check=True, capture_output=True, text=True, timeout=20,
                ).stdout.split()[1]
            if fingerprint in public_fingerprints:
                aliases = [canonical_host]
                if recovery_alias and recovery_alias not in aliases:
                    aliases.append(recovery_alias)
                return f"{','.join(aliases)} {fields[1]} {fields[2]}"
        raise RuntimeError("SSH key at the recovery address did not match the public key returned over pinned HTTPS")
    finally:
        if export_completed and not files:
            try:
                records = router_rest_request(host, tls_fingerprint, username, password, "GET", "file?.proplist=.id,name")
                files = [item for item in records if isinstance(item, dict) and str(item.get("name", "")).startswith(prefix)] if isinstance(records, list) else []
            except Exception:
                cleanup_error = True
        for item in files:
            file_id = item.get(".id")
            name = str(item.get("name", ""))
            if not re.fullmatch(re.escape(prefix) + r"_[A-Za-z0-9_.-]+", name):
                cleanup_error = True
                continue
            if file_id:
                try:
                    router_rest_request(host, tls_fingerprint, username, password, "DELETE", "file/" + urllib.parse.quote(str(file_id), safe="*"))
                except Exception:
                    try:
                        router_rest_request(host, tls_fingerprint, username, password, "POST", "execute", {
                            "script": f'/file/remove [find where name="{name}"]', "as-string": "",
                        })
                    except Exception:
                        cleanup_error = True
        if export_completed or files:
            try:
                remaining = router_rest_request(host, tls_fingerprint, username, password, "GET", "file?.proplist=.id,name")
                if not isinstance(remaining, list) or any(
                    isinstance(item, dict) and str(item.get("name", "")).startswith(prefix) for item in remaining
                ):
                    cleanup_error = True
            except Exception:
                cleanup_error = True
        if cleanup_error:
            raise RuntimeError("Could not remove and verify removal of temporary post-reset SSH host-key export files")


def fetch_verified_host_key(primary_host, recovery_host, tls_fingerprint, username, password, canonical_host):
    """Use the recovery IP only if the normal REST endpoint refuses port 443."""
    try:
        return router_ssh_host_key(primary_host, tls_fingerprint, username, password, canonical_host, recovery_host)
    except RuntimeError as exc:
        if (not recovery_host or recovery_host == primary_host
                or "ConnectionRefusedError" not in str(exc)):
            raise
        print(f"HTTPS on MIKROTIK_HOST={primary_host} refused the connection; trying MIKROTIK_IP={recovery_host} with the same saved TLS pin.")
        return router_ssh_host_key(recovery_host, tls_fingerprint, username, password, canonical_host, recovery_host)


def load_settings():
    base_url = os.environ.get("INFISICAL_URL", "").strip().rstrip("/")
    if not base_url.startswith("https://"):
        raise RuntimeError("INFISICAL_URL must use HTTPS")
    login = api_json(
        f"{base_url}/api/v1/auth/universal-auth/login",
        method="POST",
        form={
            "clientId": os.environ.get("INFISICAL_READ_CLIENT_ID", ""),
            "clientSecret": os.environ.get("INFISICAL_READ_CLIENT_SECRET", ""),
        },
    )
    token = login.get("accessToken") if isinstance(login, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError("Infisical read identity did not authenticate")

    project_id = os.environ.get("INFISICAL_PROJECT_ID", "").strip()
    environment = os.environ.get("INFISICAL_ENVIRONMENT", "").strip()
    if not project_id or not environment:
        raise RuntimeError("Infisical project ID and environment credentials are required")
    values = {}
    for path, names in {
        "/mikrotik/router01": (
            "MIKROTIK_HOST", "MIKROTIK_IP", "MIKROTIK_USERNAME", "MIKROTIK_PASSWORD",
            "MIKROTIK_BOOTSTRAP_USERNAME", "MIKROTIK_BOOTSTRAP_PASSWORD", "MIKROTIK_SSH_USER",
            "MIKROTIK_SSH_PRIVATE_KEY", "MIKROTIK_SSH_PUBLIC_KEY", "MIKROTIK_SSH_HOST_KEY",
            "MIKROTIK_TLS_CERT_SHA256",
        ),
        "/mikrotik/backup": ("BINARY_BACKUP_PASSWORD",),
    }.items():
        query = urllib.parse.urlencode({"projectId": project_id, "environment": environment, "secretPath": path})
        for name in names:
            url = f"{base_url}/api/v4/secrets/{urllib.parse.quote(name)}?{query}"
            result = api_json(url, token=token, allow_404=True)
            secret = result.get("secret", {}) if isinstance(result, dict) else {}
            value = secret.get("secretValue") if isinstance(secret, dict) else None
            values[name] = value if isinstance(value, str) else ""
    for name in ("MIKROTIK_SCRIPT",):
        query = urllib.parse.urlencode({"projectId": project_id, "environment": environment, "secretPath": "/mikrotik/router01"})
        result = api_json(f"{base_url}/api/v4/secrets/{urllib.parse.quote(name)}?{query}", token=token, allow_404=True)
        secret = result.get("secret", {}) if isinstance(result, dict) else {}
        value = secret.get("secretValue") if isinstance(secret, dict) else None
        values[name] = value if isinstance(value, str) else ""
    return values


def ros_quote(value):
    if any(char in value for char in "\r\n\0"):
        raise RuntimeError("RouterOS setting contains an unsupported line break")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$") + '"'


def verify_config_script(script, address):
    if not script.strip() or len(script.encode("utf-8")) > 512 * 1024:
        raise RuntimeError("MIKROTIK_SCRIPT must contain a non-empty RouterOS script no larger than 512 KiB")
    if "\0" in script:
        raise RuntimeError("MIKROTIK_SCRIPT contains an invalid NUL character")
    active_lines = [line.strip() for line in script.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    active_text = "\n".join(active_lines)
    if address not in active_text:
        raise RuntimeError("MIKROTIK_SCRIPT must configure MIKROTIK_IP so Jenkins can reconnect after reset")
    if not re.search(r"(?i)\bethernet2\b|\bether2\b", active_text):
        raise RuntimeError("MIKROTIK_SCRIPT must configure or reference the Proxmox-connected ether2 interface")
    if re.search(r"(?im)^\s*/system\s+reset-configuration\b", active_text):
        raise RuntimeError("MIKROTIK_SCRIPT must not contain another reset-configuration command")

    section = ""
    sanitized = []
    skipped_user_section = False
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("/"):
            words = stripped.split(None, 2)
            section = " ".join(words[:2]).lower() if len(words) > 1 else stripped.lower()
            skipped_user_section = section.startswith("/user")
            if not skipped_user_section:
                sanitized.append(line)
        elif not skipped_user_section:
            sanitized.append(line)
    script = "\n".join(sanitized).strip() + "\n"
    sanitized_active = "\n".join(line.strip() for line in script.splitlines() if line.strip() and not line.lstrip().startswith("#"))
    if address not in sanitized_active:
        raise RuntimeError("MIKROTIK_SCRIPT must set MIKROTIK_IP outside its /user configuration sections so Jenkins can reconnect")
    if not re.search(r"(?i)\bethernet2\b|\bether2\b", sanitized_active):
        raise RuntimeError("MIKROTIK_SCRIPT must configure or reference the Proxmox-connected ether2 interface outside its /user sections")
    return script


def known_host_key(value, host):
    lines = [line.strip() for line in value.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if len(lines) != 1:
        raise RuntimeError("MIKROTIK_SSH_HOST_KEY must contain one independently verified known_hosts entry")
    fields = lines[0].split()
    aliases = fields[0].split(",") if fields else []
    if len(fields) < 3 or host not in aliases or not fields[1].startswith("ssh-"):
        raise RuntimeError("MIKROTIK_SSH_HOST_KEY does not match MIKROTIK_HOST")
    entry = HostKeyEntry.from_line(" ".join(fields))
    if entry is None:
        raise RuntimeError("Could not parse the pinned MikroTik SSH host key")
    return fields[1], entry.key


def connect(host, user, host_key, *, password=None, key_filename=None, timeout=15):
    key_type, pinned_key = host_key
    client = paramiko.SSHClient()
    client.get_host_keys().add(host, key_type, pinned_key)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    options = {
        "hostname": host,
        "username": user,
        "look_for_keys": False,
        "allow_agent": False,
        "timeout": timeout,
        "banner_timeout": timeout,
        "auth_timeout": timeout,
    }
    if password is not None:
        options["password"] = password
    else:
        options["key_filename"] = key_filename
    try:
        client.connect(**options)
    except paramiko.BadHostKeyException:
        client.close()
        raise RuntimeError("MikroTik SSH host key did not match the independently saved pin") from None
    except paramiko.AuthenticationException:
        client.close()
        raise RuntimeError("MikroTik rejected the configured SSH username or credentials") from None
    except Exception:
        client.close()
        raise RuntimeError("MikroTik SSH is not reachable or its SSH handshake did not complete") from None
    return client


def connect_router(primary_host, recovery_host, user, host_key, *, password=None, key_filename=None, timeout=15):
    """Try the normal management name, then the configured recovery address for reachability."""
    hosts = [primary_host]
    if recovery_host and recovery_host != primary_host:
        hosts.append(recovery_host)
    last_error = None
    for index, host in enumerate(hosts):
        try:
            return connect(host, user, host_key, password=password, key_filename=key_filename, timeout=timeout)
        except RuntimeError as exc:
            last_error = exc
            detail = str(exc).lower()
            if "host key did not match" in detail or "rejected the configured ssh username" in detail:
                raise
            if index == len(hosts) - 1:
                raise
    raise last_error or RuntimeError("MikroTik SSH is not reachable")


def command(client, value, *, allow_disconnect=False):
    try:
        _stdin, stdout, stderr = client.exec_command(value, timeout=45)
        output = stdout.read().decode("utf-8", errors="replace")
        error = stderr.read().decode("utf-8", errors="replace")
        status = stdout.channel.recv_exit_status()
    except Exception:
        if allow_disconnect:
            return ""
        raise RuntimeError("RouterOS command failed; command output was suppressed") from None
    if not allow_disconnect and (status != 0 or re.search(r"(?im)^\s*(failure|error|script error):|found [1-9][0-9]* error", output + "\n" + error)):
        raise RuntimeError("RouterOS rejected a command; command output was suppressed")
    return output


def save_backup(client, password):
    build_number = os.environ.get("BUILD_NUMBER", "").strip()
    if not re.fullmatch(r"[0-9]+", build_number):
        raise RuntimeError("Jenkins BUILD_NUMBER is missing or invalid")
    artifact_dir = Path("artifacts") / build_number
    artifact_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(artifact_dir, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"homelab-router-before-reset-{build_number}-{stamp}-{secrets.token_hex(4)}.backup"
    final_path = artifact_dir / name
    temporary_path = artifact_dir / (name + ".part")
    command(client, f"/system/backup/save name={name} password={ros_quote(password)}")
    download_failed = False
    try:
        sftp = client.open_sftp()
        try:
            try:
                remote_path = "/" + name
                remote_stat = sftp.stat(remote_path)
            except OSError:
                remote_path = name
                remote_stat = sftp.stat(remote_path)
            if remote_stat.st_size <= 0:
                raise RuntimeError("RouterOS created an empty encrypted pre-reset backup")
            sftp.get(remote_path, str(temporary_path))
        finally:
            sftp.close()
        if not temporary_path.is_file() or temporary_path.stat().st_size != remote_stat.st_size:
            raise RuntimeError("Downloaded pre-reset backup did not match the RouterOS file size")
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, final_path)
    except Exception:
        download_failed = True
        try:
            temporary_path.unlink()
        except OSError:
            pass

    cleanup_failed = False
    try:
        command(client, f"/file/remove [find where name={ros_quote(name)}]", allow_disconnect=False)
        sftp = client.open_sftp()
        try:
            remaining = {entry.rsplit("/", 1)[-1] for entry in sftp.listdir(".")}
        finally:
            sftp.close()
        if name in remaining:
            raise RuntimeError("RouterOS still lists the temporary backup file")
    except Exception:
        cleanup_failed = True

    if download_failed and cleanup_failed:
        raise RuntimeError(
            "Encrypted router backup could not be downloaded and removal of its temporary router copy could not be verified; reset was not started"
        ) from None
    if download_failed:
        raise RuntimeError("Encrypted router backup could not be downloaded and verified; reset was not started") from None
    if cleanup_failed:
        raise RuntimeError(
            "Could not verify removal of the temporary RouterOS backup; reset was not started and the encrypted Jenkins artifact was retained"
        ) from None
    print(f"Downloaded encrypted pre-reset backup as Jenkins artifact {name}.")


def upload_text(client, remote_name, content):
    sftp = client.open_sftp()
    try:
        with sftp.file(remote_name, "wb") as target:
            target.write(content.encode("utf-8"))
            target.flush()
        if sftp.stat(remote_name).st_size != len(content.encode("utf-8")):
            raise RuntimeError("Uploaded RouterOS script size did not match the Infisical source")
    finally:
        sftp.close()


def build_wrapper(config_path, wrapper_path, host_key_path, host_key_passphrase, marker, admin_user, admin_password, ssh_user, ssh_public_key):
    lines = [":delay 10s"]
    user = ros_quote(admin_user)
    password = ros_quote(admin_password)
    lines.append(f":if ([:len [/user find where name={user}]] = 0) do={{ /user add name={user} group=full password={password} }} else={{ /user set [find where name={user}] group=full password={password} }}")
    if ssh_user != admin_user:
        ssh_name = ros_quote(ssh_user)
        lines.append(f":if ([:len [/user find where name={ssh_name}]] = 0) do={{ /user add name={ssh_name} group=full }} else={{ /user set [find where name={ssh_name}] group=full }}")
    lines.extend((
        f"/user ssh-keys add user={ros_quote(ssh_user)} key={ros_quote(ssh_public_key)}",
        f"/import file-name={config_path} verbose=yes",
        f"/ip/ssh/import-host-key private-key-file={ros_quote(host_key_path)} passphrase={ros_quote(host_key_passphrase)}",
        f"/file/remove [find where name={ros_quote(host_key_path)}]",
        *(['/user disable [find where name="admin"]'] if admin_user != "admin" else []),
        f':log info "{marker}"',
        f"/file/remove [find where name={ros_quote(config_path)}]",
        f"/file/remove [find where name={ros_quote(wrapper_path)}]",
    ))
    return "\n".join(lines) + "\n"


def preserve_router_host_key(client, remote_dir, build_number, key_type):
    """Export the currently pinned router host key and stage an encrypted copy for run-after-reset."""
    suffix = {"ssh-rsa": "rsa", "ssh-ed25519": "ed25519"}.get(key_type)
    if not suffix:
        raise RuntimeError(f"Unsupported saved MikroTik SSH host-key type: {key_type}")
    prefix = f"homelab-preserved-hostkey-{build_number}"
    passphrase = secrets.token_urlsafe(32)
    command(client, f"/ip/ssh/export-host-key key-file-prefix={prefix} passphrase={ros_quote(passphrase)}")

    exported_names = []
    sftp = client.open_sftp()
    try:
        names = sftp.listdir(".")
        exported_names = [name for name in names if name == prefix or name.startswith(prefix + "_")]
        candidates = [
            name for name in exported_names
            if name in (f"{prefix}_{suffix}", f"{prefix}_{suffix}.pem")
        ]
        if len(candidates) != 1:
            raise RuntimeError("RouterOS did not produce exactly one private host-key file matching the saved pin")

        source_name = candidates[0]
        with sftp.file(source_name, "rb") as source:
            private_key = source.read()
        if not private_key:
            raise RuntimeError("RouterOS exported an empty SSH host-key file")

        target_name = f"{remote_dir}homelab-restore-hostkey-{build_number}-{suffix}.pem"
        with sftp.file(target_name, "wb") as target:
            target.write(private_key)
            target.flush()
        sftp.chmod(target_name, 0o600)
        if sftp.stat(target_name).st_size != len(private_key):
            raise RuntimeError("Staged encrypted SSH host-key size did not match the export")
    except Exception:
        raise RuntimeError("Could not securely preserve the pinned MikroTik SSH host key; reset was not started") from None
    finally:
        for name in exported_names:
            try:
                sftp.remove(name)
            except OSError:
                pass
        sftp.close()

    return target_name, passphrase


def verify_and_reconnect(address, canonical_host, ssh_user, key_file, tls_fingerprint, rest_user, rest_password, marker, deadline):
    last_error = "Router has not returned yet"
    next_status = time.monotonic()
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        client = None
        try:
            host_key_line = router_ssh_host_key(
                address, tls_fingerprint, rest_user, rest_password, canonical_host, address,
            )
            save_verified_host_key(host_key_line, bool(values.get("MIKROTIK_SSH_HOST_KEY", "")))
            values["MIKROTIK_SSH_HOST_KEY"] = host_key_line
            host_key = known_host_key(host_key_line, canonical_host)
            print("Post-reset SSH host key matched the key returned over the pinned HTTPS connection and was saved to Infisical.")
            client = connect(address, ssh_user, host_key, key_filename=key_file, timeout=8)
            command(client, "/system/resource/get version")
            command(client, "/ip/address/print")
            marker_count = command(client, f"/log/print count-only where message={ros_quote(marker)}").strip()
            if marker_count != "1":
                raise RuntimeError("Router is reachable, but the complete configuration did not report successful import")
            client.close()
            print(f"Router restored and verified at {address}; Jenkins key authentication is working.")
            return
        except Exception as exc:
            # Network, SSH handshake, and RouterOS command failures are
            # expected while the router is rebooting and importing its config.
            last_error = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
            if client:
                try:
                    client.close()
                except Exception:
                    pass
            if any(marker in last_error.lower() for marker in (
                "does not match the saved tls pin",
                "did not match the public key returned over pinned https",
                "http 401",
                "http 403",
                "infisical could not save",
                "infisical did not confirm",
            )):
                raise RuntimeError(f"Post-reset SSH host-key trust update stopped safely: {last_error}") from None
            time.sleep(15)
        if time.monotonic() >= next_status:
            print(f"Waiting for MikroTik recovery at {address}: SSH not ready yet (attempt {attempts}; last status: {last_error}).")
            next_status = time.monotonic() + 60
    raise RuntimeError(f"Reset was initiated but the router did not return with verified HTTPS and SSH access at MIKROTIK_IP={address} before the recovery timeout. Check the Proxmox-connected ether2 link, ensure www-ssl serves the saved TLS certificate, and check the script log on the router. Last safe status: {last_error}")


def main():
    arguments = sys.argv[1:]
    if arguments not in ([], ["--check"]):
        raise RuntimeError("Usage: restore-mikrotik.py [--check]")
    check_only = arguments == ["--check"]
    values = load_settings()
    current_host = required(values, "MIKROTIK_HOST")
    address = str(ipaddress.IPv4Address(required(values, "MIKROTIK_IP")))
    bootstrap_user = required(values, "MIKROTIK_BOOTSTRAP_USERNAME")
    admin_user = required(values, "MIKROTIK_USERNAME")
    ssh_user = required(values, "MIKROTIK_SSH_USER")
    if not all(re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", user) for user in (bootstrap_user, admin_user, ssh_user)):
        raise RuntimeError("MikroTik usernames must contain only letters, digits, underscore, period, or hyphen")
    bootstrap_password = required(values, "MIKROTIK_BOOTSTRAP_PASSWORD")
    admin_password = required(values, "MIKROTIK_PASSWORD")
    tls_fingerprint = required(values, "MIKROTIK_TLS_CERT_SHA256").lower().replace(":", "").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", tls_fingerprint):
        raise RuntimeError("MIKROTIK_TLS_CERT_SHA256 must be a 64-character SHA-256 fingerprint before Job 005 can verify or update SSH host keys")
    backup_password = required(values, "BINARY_BACKUP_PASSWORD")
    if any(len(value) < 8 for value in (bootstrap_password, admin_password, backup_password)):
        raise RuntimeError("MikroTik bootstrap, admin, and backup passwords must each be at least 8 characters")
    private_key = required(values, "MIKROTIK_SSH_PRIVATE_KEY")
    public_key = required(values, "MIKROTIK_SSH_PUBLIC_KEY")
    script = verify_config_script(required(values, "MIKROTIK_SCRIPT"), address)
    if not private_key.startswith("-----BEGIN OPENSSH PRIVATE KEY-----") or not public_key.startswith("ssh-ed25519 "):
        raise RuntimeError("Run Job 003 first to generate the MikroTik Ed25519 SSH client key pair")
    saved_host_key = values.get("MIKROTIK_SSH_HOST_KEY", "").strip()
    try:
        host_key = known_host_key(saved_host_key, current_host) if saved_host_key else None
    except RuntimeError:
        host_key = None
    if any(char in value for value in (backup_password, admin_password, bootstrap_password) for char in "\r\n\0"):
        raise RuntimeError("MikroTik passwords must not contain line breaks or NUL characters")

    key_path = Path("mikrotik-restore-key")
    key_path.write_text(private_key.rstrip("\n") + "\n", encoding="utf-8")
    os.chmod(key_path, 0o600)
    client = None
    try:
        try:
            saved_private_key = paramiko.Ed25519Key.from_private_key_file(str(key_path))
        except Exception:
            raise RuntimeError("MIKROTIK_SSH_PRIVATE_KEY is not a valid Ed25519 private key; reset was not started") from None
        public_key_lines = public_key.splitlines()
        public_key_fields = public_key_lines[0].split() if len(public_key_lines) == 1 else []
        if (
            len(public_key_fields) < 2
            or public_key_fields[0] != "ssh-ed25519"
            or saved_private_key.get_base64() != public_key_fields[1]
        ):
            raise RuntimeError("Saved MikroTik SSH public and private keys do not match; reset was not started")
        stale_host_key = host_key is None
        if host_key is not None:
            try:
                client = connect_router(current_host, address, bootstrap_user, host_key, password=bootstrap_password)
            except RuntimeError as exc:
                if "host key did not match" not in str(exc).lower():
                    raise
                stale_host_key = True
        if stale_host_key:
            current_host_key = fetch_verified_host_key(
                current_host, address, tls_fingerprint, admin_user, admin_password, current_host,
            )
            host_key = known_host_key(current_host_key, current_host)
            if check_only:
                print("Readiness check passed: HTTPS verified the current SSH host key against the saved TLS certificate pin.")
                print("After approval, Job 005 will save this verified key to Infisical and confirm SSH access before creating the backup or resetting the router.")
                print(f"Recovery address: {address} over the configured Proxmox-connected ether2 path.")
                return
            save_verified_host_key(current_host_key, bool(saved_host_key))
            values["MIKROTIK_SSH_HOST_KEY"] = current_host_key
            print("Updated the saved MikroTik SSH host key in Infisical after verifying it through the pinned HTTPS connection.")
            client = connect_router(current_host, address, bootstrap_user, host_key, password=bootstrap_password)
        if check_only:
            resource = command(client, "/system/resource print")
            version = re.search(r"(?im)^version:\s*(\S+)", resource)
            print("Readiness check passed: pinned SSH connection and reset/restore settings are valid.")
            print(f"RouterOS: {version.group(1) if version else 'version unavailable'}")
            print(f"Recovery address: {address} over the configured Proxmox-connected ether2 path.")
            print("No backup or router changes were made. The encrypted backup and RouterOS dry-run happen after approval.")
            return
        save_backup(client, backup_password)

        build_number = os.environ.get("BUILD_NUMBER", "")
        config_name = f"homelab-config-{build_number}.rsc"
        wrapper_name = f"homelab-reset-{build_number}.rsc"
        marker = f"homelab-restore-success-{build_number}"
        # On RouterOS devices exposing a flash directory, place the one-shot
        # files there so they survive reset/reboot.
        sftp = client.open_sftp()
        try:
            entries = sftp.listdir(".")
            remote_dir = "flash/" if "flash" in entries else ""
        finally:
            sftp.close()
        config_path = remote_dir + config_name
        wrapper_path = remote_dir + wrapper_name
        host_key_path, host_key_passphrase = preserve_router_host_key(
            client, remote_dir, build_number, host_key[0]
        )
        wrapper = build_wrapper(
            config_path, wrapper_path, host_key_path, host_key_passphrase,
            marker, admin_user, admin_password, ssh_user, public_key,
        )
        upload_text(client, config_path, script)
        upload_text(client, wrapper_path, wrapper)
        command(client, f"/import file-name={config_path} verbose=yes dry-run")
        print("RouterOS dry-run accepted the complete configuration script.")

        # Save a one-shot RouterOS script which performs the reset from script
        # context (avoids an interactive confirmation prompt on SSH exec).
        reset_script = f"/system/reset-configuration no-defaults=yes skip-backup=yes run-after-reset={wrapper_path}"
        trigger_name = f"homelab-reset-trigger-{build_number}"
        command(client, f"/system/script/add name={trigger_name} source={ros_quote(reset_script)}")
        print(f"Starting the approved full reset. Jenkins will reconnect to {address} over the configured ether2 path.")
        command(client, f"/system/script/run [find where name={trigger_name}]", allow_disconnect=True)
        client.close()
        client = None
        # RouterOS reset, first boot, and configuration import can take
        # several minutes. Keep polling well beyond the usual restart window.
        deadline = time.monotonic() + 30 * 60
        verify_and_reconnect(
            address, current_host, ssh_user, str(key_path), tls_fingerprint,
            admin_user, admin_password, marker, deadline,
        )
        # Apply Infisical-managed DNS/DHCP mode and Wi-Fi passphrases over the
        # restored key-authenticated connection, with its own encrypted backup.
        followup_env = os.environ.copy()
        # The reset-only ether2 address can differ from the usual management
        # endpoint. Reuse the same pinned router key, but connect to recovery IP.
        followup_env["HOMELAB_MIKROTIK_CONNECT_HOST"] = address
        followup = subprocess.run(
            [sys.executable, "jenkins/pipelines/mikrotik-config/apply-mikrotik-config.py"],
            check=False,
            timeout=10 * 60,
            env=followup_env,
        )
        if followup.returncode:
            raise RuntimeError("Router reset and full-script import succeeded, but the Infisical DNS/Wi-Fi follow-up failed; encrypted backups are available in this Jenkins build")
    finally:
        if client:
            client.close()
        try:
            key_path.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
