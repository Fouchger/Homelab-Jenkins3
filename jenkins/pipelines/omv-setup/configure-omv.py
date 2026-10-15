#!/usr/bin/env python3
"""Reconcile the OMV NFS export required by the Talos Kubernetes cluster."""

import http.cookiejar
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request


OMV_URL = os.environ.get("OMV_URL", "http://192.168.70.20").rstrip("/")
OMV_FS_LABEL = "TB4a"
SHARE_NAME = "Kubernetes"
NFS_CLIENT = "192.168.80.0/24"
NFS_OPTIONS = "rw"
NFS_EXTRA_OPTIONS = "sync,no_subtree_check,no_root_squash"
NFS_COMMENT = "Dedicated Kubernetes persistent volume export"
ACTION = os.environ.get("OMV_ACTION", "plan").strip().lower()


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required setting {name} is missing")
    return value


def infisical_json(url, *, method="GET", token=None, form=None):
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
            payload = response.read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        raise RuntimeError("Infisical request failed; response suppressed") from None
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("Infisical returned invalid JSON") from None


def read_infisical_secret(token, name):
    query = urllib.parse.urlencode({
        "projectId": required("INFISICAL_PROJECT_ID"),
        "environment": required("INFISICAL_ENVIRONMENT"),
        "secretPath": "/omv",
    })
    url = f"{required('INFISICAL_URL').rstrip('/')}/api/v4/secrets/{urllib.parse.quote(name)}?{query}"
    result = infisical_json(url, token=token)
    secret = result.get("secret") if isinstance(result, dict) else None
    value = secret.get("secretValue") if isinstance(secret, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Infisical secret /omv/{name} is missing or empty")
    return value


class OmvRpc:
    def __init__(self, username, password):
        if not OMV_URL.startswith(("http://", "https://")):
            raise RuntimeError("OMV_URL must use HTTP or HTTPS")
        parsed = urllib.parse.urlsplit(OMV_URL)
        if parsed.hostname not in {"192.168.70.20", "omv01.fouchger.uk"} or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RuntimeError("OMV_URL must target 192.168.70.20 or omv01.fouchger.uk without embedded credentials or query parameters")
        if OMV_URL.startswith("http://") and os.environ.get("OMV_ALLOW_INSECURE_HTTP", "false").lower() != "true":
            raise RuntimeError("OMV is configured for HTTP. Enable ALLOW_INSECURE_HTTP only on the trusted management LAN, or configure OMV HTTPS first.")
        self.url = f"{OMV_URL}/rpc.php"
        self.cookies = http.cookiejar.CookieJar()
        handlers = [urllib.request.HTTPCookieProcessor(self.cookies)]
        if OMV_URL.startswith("https://"):
            handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        self.opener = urllib.request.build_opener(*handlers)
        self.call("session", "login", {"username": username, "password": password}, login=True)

    def call(self, service, method, params=None, *, login=False):
        body = json.dumps({"service": service, "method": method, "params": params}).encode()
        request = urllib.request.Request(
            self.url, data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=45) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError(f"OMV RPC {service}.{method} failed; response suppressed") from None
        if not isinstance(result, dict) or result.get("error"):
            # OMV errors can contain account and configuration details. Do not echo them to Jenkins.
            raise RuntimeError(f"OMV RPC {service}.{method} returned an error")
        if login and not result.get("response"):
            raise RuntimeError("OMV web administrator authentication failed")
        return result.get("response")

    def close(self):
        try:
            self.call("session", "logout", None)
        except RuntimeError:
            pass


def rpc_list(rpc, service, method, params):
    result = rpc.call(service, method, params)
    if isinstance(result, dict) and isinstance(result.get("data"), list):
        return result["data"]
    if isinstance(result, list):
        return result
    raise RuntimeError(f"OMV {service}.{method} returned an unexpected list")


def plan(rpc):
    filesystems = rpc.call("FileSystemMgmt", "enumerateMountedFilesystems", {"includeroot": False})
    if not isinstance(filesystems, list):
        raise RuntimeError("OMV did not return the mounted filesystem inventory")
    targets = [item for item in filesystems if item.get("label") == OMV_FS_LABEL]
    if len(targets) != 1:
        labels = sorted({str(item.get("label") or "(unlabelled)") for item in filesystems})
        raise RuntimeError(f"Expected one mounted filesystem labelled {OMV_FS_LABEL}; found {len(targets)}. Mounted labels: {', '.join(labels)}. No changes made.")
    filesystem = targets[0]
    mountpoints = rpc_list(rpc, "Config", "getlist", {
        "id": "conf.system.filesystem.mountpoint", "start": 0, "limit": -1,
        "sortfield": "dir", "sortdir": "ASC",
    })
    configured_mounts = [item for item in mountpoints if item.get("dir") == filesystem.get("mountpoint")]
    if len(configured_mounts) != 1:
        raise RuntimeError("Could not uniquely map the mounted TB4a filesystem to its OMV mountpoint configuration")
    mountpoint_ref = configured_mounts[0].get("uuid")
    if not isinstance(mountpoint_ref, str) or not mountpoint_ref:
        raise RuntimeError("The TB4a filesystem mountpoint has no OMV configuration reference")

    folders = rpc_list(rpc, "Config", "getlist", {
        "id": "conf.system.sharedfolder", "start": 0, "limit": -1,
        "sortfield": "name", "sortdir": "ASC",
    })
    named_folders = [folder for folder in folders if folder.get("name") == SHARE_NAME]
    if len(named_folders) > 1:
        raise RuntimeError("OMV has duplicate Kubernetes shared-folder definitions; no changes made")
    folder = named_folders[0] if named_folders else None
    folder_change = folder is None
    if folder and (folder.get("mntentref") != mountpoint_ref or folder.get("reldirpath", "").strip("/") != SHARE_NAME):
        raise RuntimeError("The existing Kubernetes shared folder is not on TB4a/Kubernetes; refusing to move or overwrite it")

    settings = rpc.call("NFS", "getSettings", {})
    if not isinstance(settings, dict):
        raise RuntimeError("OMV did not return NFS settings")
    versions = settings.get("versions", [])
    if not isinstance(versions, list):
        raise RuntimeError("OMV returned an unexpected NFS version list")
    desired_versions = list(dict.fromkeys([*versions, "4.1"]))
    settings_change = not settings.get("enable") or versions != desired_versions

    shares = rpc_list(rpc, "NFS", "getShareList", {
        "start": 0, "limit": -1, "sortfield": "sharedfoldername", "sortdir": "ASC",
    })
    target_shares = [share for share in shares if share.get("sharedfoldername") == SHARE_NAME]
    if len(target_shares) > 1:
        raise RuntimeError("OMV has duplicate NFS exports for the Kubernetes folder; no changes made")
    desired = {
        "client": NFS_CLIENT,
        "options": NFS_OPTIONS,
        "extraoptions": NFS_EXTRA_OPTIONS,
    }
    current = target_shares[0] if target_shares else None
    share_change = current is None or any(current.get(key, "") != value for key, value in desired.items())

    print(f"OMV RPC authenticated; mounted data filesystem: {OMV_FS_LABEL} at {filesystem.get('mountpoint', '(mountpoint not reported)')}", flush=True)
    print(f"NFS service: {'enabled' if settings.get('enable') else 'disabled'}; versions currently {','.join(versions) or '(none)'}", flush=True)
    if current:
        print(f"Kubernetes export: client={current.get('client')}, access={current.get('options')}, extra={current.get('extraoptions')}", flush=True)
    else:
        print("Kubernetes NFS export: missing", flush=True)
    changes = []
    if folder_change:
        changes.append("create shared folder Kubernetes on TB4a at relative path Kubernetes (directory creation only; no formatting)")
    if settings_change:
        changes.append("enable NFS and ensure NFSv4.1 is enabled while preserving other enabled protocol versions")
    if share_change:
        changes.append("set the Kubernetes export to 192.168.80.0/24, read/write, sync,no_subtree_check,no_root_squash")
    if not changes:
        print("OMV already matches the desired NFS configuration; no changes are planned.", flush=True)
    else:
        for change in changes:
            print(f"Plan: {change}", flush=True)
    return filesystem, mountpoint_ref, folder, settings, desired_versions, current, desired, changes


def apply(rpc, state):
    filesystem, mountpoint_ref, folder, settings, versions, current, desired, changes = state
    if folder is None:
        created = rpc.call("ShareMgmt", "set", {
            "name": SHARE_NAME,
            "comment": NFS_COMMENT,
            "mntentref": mountpoint_ref,
            "reldirpath": SHARE_NAME,
            "mode": "775",
        })
        folder = created
        if not isinstance(folder, dict) or not folder.get("uuid"):
            raise RuntimeError("OMV did not confirm creation of the Kubernetes shared folder")
        print("Created OMV shared-folder metadata and directory on the mounted TB4a filesystem.", flush=True)

    if not settings.get("enable") or settings.get("versions") != versions:
        rpc.call("NFS", "setSettings", {"enable": True, "versions": versions})

    if current is None:
        rpc.call("NFS", "setShare", {
            "sharedfolderref": folder["uuid"],
            "client": desired["client"],
            "options": desired["options"],
            "extraoptions": desired["extraoptions"],
            "comment": NFS_COMMENT,
        })
    elif any(current.get(key, "") != value for key, value in desired.items()):
        rpc.call("NFS", "setShare", {
            "uuid": current["uuid"],
            "sharedfolderref": current["sharedfolderref"],
            "mntentref": current["mntentref"],
            "client": desired["client"],
            "options": desired["options"],
            "extraoptions": desired["extraoptions"],
            "comment": NFS_COMMENT,
        })

    if changes:
        rpc.call("Config", "applyChanges", {"modules": ["fstab", "sharedfolders", "nfs"], "force": False})
        print("Applied OMV fstab, shared-folder, and NFS modules.", flush=True)
    verified = plan(rpc)
    if verified[-1]:
        raise RuntimeError("OMV did not converge to the desired state after apply")
    stats = rpc.call("NFS", "getStats", {})
    print("OMV NFS configuration verified. Export status:", flush=True)
    if isinstance(stats, str):
        for line in stats.splitlines():
            if SHARE_NAME in line or NFS_CLIENT in line:
                print(line, flush=True)
    print("NFS reconciliation completed successfully.", flush=True)


def main():
    if ACTION not in {"plan", "apply"}:
        raise RuntimeError("OMV_ACTION must be 'plan' or 'apply'")
    base = required("INFISICAL_URL").rstrip("/")
    if not base.startswith("https://"):
        raise RuntimeError("INFISICAL_URL must use HTTPS")
    login = infisical_json(
        f"{base}/api/v1/auth/universal-auth/login", method="POST",
        form={"clientId": required("INFISICAL_READ_CLIENT_ID"),
              "clientSecret": required("INFISICAL_READ_CLIENT_SECRET")},
    )
    token = login.get("accessToken") if isinstance(login, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError("Infisical read identity authentication failed")
    username = read_infisical_secret(token, "OMV_USER")
    password = read_infisical_secret(token, "OMV_USER_PASSWORD")
    rpc = OmvRpc(username, password)
    try:
        state = plan(rpc)
        if ACTION == "apply":
            if state[-1]:
                apply(rpc, state)
            else:
                print("OMV is already converged; nothing to apply.", flush=True)
    finally:
        rpc.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"OMV configuration failed: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
