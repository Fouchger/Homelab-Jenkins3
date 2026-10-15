#!/usr/bin/env python3
"""Inventory OMV and safely restore/reconcile its data-service configuration."""

import http.cookiejar
import json
import os
import re
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
INVENTORY_PATH = os.environ.get("OMV_INVENTORY_PATH", "omv-inventory.json")
RESTORE_SECRET = "OMV_RESTORE_INVENTORY"
EXPECTED_DATA_FILESYSTEMS = {
    "TB10a": "cd3bca8a-831d-4432-adc0-92e2aedf8569",
    "TB10b": "c13be974-e9e3-4ffa-b249-568670e928c2",
    "TB5a": "86fcd23a-1b7f-487f-a0eb-bb16849debdf",
    "TB4a": "2e2b5958-abf6-4272-93c2-9b198aa9e67f",
    "TB16a": "b408b5f4-1f6d-496d-b7ad-7a19aaf5fb5d",
}
EXPECTED_PROXMOX_SERIALS = {
    "scsi1": "WP00WBAP",
    "scsi2": "WP014X13",
    "scsi3": "WCJ6V5MV",
    "scsi4": "WCK0DD4S",
    "scsi5": "ZYD83XZA",
}


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


def read_infisical_secret(token, name, secret_path="/omv"):
    query = urllib.parse.urlencode({
        "projectId": required("INFISICAL_PROJECT_ID"),
        "environment": required("INFISICAL_ENVIRONMENT"),
        "secretPath": secret_path,
    })
    url = f"{required('INFISICAL_URL').rstrip('/')}/api/v4/secrets/{urllib.parse.quote(name)}?{query}"
    result = infisical_json(url, token=token)
    secret = result.get("secret") if isinstance(result, dict) else None
    value = secret.get("secretValue") if isinstance(secret, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Infisical secret {secret_path.rstrip('/')}/{name} is missing or empty")
    return value


def read_proxmox_vm_config():
    """Read the existing OMV VM configuration for the rebuild inventory."""
    host = required("PROXMOX_API_HOSTNAME")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        raise RuntimeError("PROXMOX_API_HOSTNAME must be a DNS hostname or IPv4 address")
    base = required("INFISICAL_URL").rstrip("/")
    login = infisical_json(
        f"{base}/api/v1/auth/universal-auth/login", method="POST",
        form={"clientId": required("INFISICAL_READ_CLIENT_ID"),
              "clientSecret": required("INFISICAL_READ_CLIENT_SECRET")},
    )
    token = login.get("accessToken") if isinstance(login, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError("Infisical read identity authentication failed")
    token_id = read_infisical_secret(token, "PVE_API_TOKEN_ID", "/proxmox/automation")
    token_secret = read_infisical_secret(token, "PVE_API_TOKEN_SECRET", "/proxmox/automation")
    if "!" not in token_id:
        raise RuntimeError("Infisical PVE_API_TOKEN_ID is not a fully qualified Proxmox token ID")

    url = f"https://{host}:8006/api2/json/nodes/pve01/qemu/22000/config"
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "Authorization": f"PVEAPIToken={token_id}={token_secret}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ssl.SSLError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("Could not read the OMV Proxmox VM configuration; response suppressed") from None
    config = result.get("data") if isinstance(result, dict) else None
    if not isinstance(config, dict) or config.get("name") != "omv01":
        raise RuntimeError("Proxmox VMID 22000 is not the expected omv01 VM")
    required_devices = {"scsi0", "scsi1", "scsi2", "scsi3", "scsi4", "scsi5", "net0", "net1"}
    missing_devices = sorted(required_devices.difference(config))
    if missing_devices:
        raise RuntimeError(
            "Proxmox OMV VM inventory is incomplete; missing hardware entries: "
            + ", ".join(missing_devices)
        )

    # Allowlist only non-secret properties needed to reproduce and verify the
    # guest. Proxmox cloud-init fields and arbitrary hook/argument data are not
    # copied into a Jenkins artifact.
    keys = {
        "name", "memory", "cores", "sockets", "cpu", "bios", "machine",
        "scsihw", "ide2", "scsi0", "scsi1", "scsi2", "scsi3", "scsi4",
        "scsi5", "net0", "net1", "boot", "bootdisk", "agent", "startup",
        "protection", "ostype", "onboot", "tags", "tablet", "acpi", "kvm",
    }
    return {key: config[key] for key in sorted(keys) if key in config}


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


def config_list(rpc, config_id, sortfield=None):
    params = {"id": config_id, "start": 0, "limit": -1}
    if sortfield:
        params.update({"sortfield": sortfield, "sortdir": "ASC"})
    return rpc_list(rpc, "Config", "getlist", params)


def load_restore_inventory(token):
    """Load the pre-rebuild, password-redacted capture saved by the operator."""
    raw = read_infisical_secret(token, RESTORE_SECRET, "/omv")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"Infisical /omv/{RESTORE_SECRET} must contain the complete JSON from a successful INVENTORY build") from None
    if not isinstance(document, dict) or document.get("schemaVersion") != 3:
        raise RuntimeError("OMV restore inventory schema is not supported; recapture with this Job 011 version")
    return document


def filesystem_identity_matches(filesystem, expected_uuid):
    return str(filesystem.get("uuid", "")).lower() == expected_uuid.lower()


def filesystem_aliases(filesystem):
    aliases = set()
    for key in ("uuid", "devicefile", "canonicaldevicefile", "predictabledevicefile", "devicename"):
        value = filesystem.get(key)
        if isinstance(value, str) and value:
            aliases.add(value.casefold())
            if key == "devicename" and not value.startswith("/dev/"):
                aliases.add(f"/dev/{value}".casefold())
    for key in ("devlinks", "devicefiles"):
        values = filesystem.get(key, [])
        if isinstance(values, list):
            aliases.update(str(value).casefold() for value in values if value)
    return aliases


def mount_config_matches_filesystem(mount_config, filesystem):
    fsname = str(mount_config.get("fsname", "")).casefold()
    uuid = str(filesystem.get("uuid", "")).casefold()
    return fsname in filesystem_aliases(filesystem) or (uuid and uuid in fsname)


def source_mount_to_label(inventory):
    """Relate old OMV mountpoint UUIDs to the captured filesystem labels."""
    labels_by_fsname = {}
    labels_by_uuid = {}
    for filesystem in inventory.get("filesystems", []):
        if not isinstance(filesystem, dict) or not filesystem.get("uuid"):
            continue
        labels_by_uuid[str(filesystem["uuid"]).casefold()] = filesystem.get("label")
        for key in ("devicefile", "canonicaldevicefile", "predictabledevicefile", "uuid"):
            value = filesystem.get(key)
            if isinstance(value, str) and value:
                labels_by_fsname[value] = filesystem.get("label")
        for value in filesystem.get("devlinks", []) if isinstance(filesystem.get("devlinks"), list) else []:
            if value:
                labels_by_fsname[value] = filesystem.get("label")
    mount_labels = {}
    for mount in inventory.get("mountpoints", []):
        if isinstance(mount, dict):
            fsname = str(mount.get("fsname", ""))
            label = labels_by_fsname.get(fsname)
            if not label:
                label = next((value for uuid, value in labels_by_uuid.items() if uuid and uuid in fsname.casefold()), None)
            if label:
                mount_labels[mount.get("uuid")] = label
    return mount_labels


def restore_plan(rpc, inventory, token, current_proxmox_vm):
    """Validate every data identity and compute the safe configuration restore."""
    captured = {
        item.get("label"): item.get("uuid")
        for item in inventory.get("filesystems", [])
        if isinstance(item, dict) and item.get("label")
    }
    if any(captured.get(label) != uuid for label, uuid in EXPECTED_DATA_FILESYSTEMS.items()):
        raise RuntimeError("Saved inventory does not contain the five verified data filesystem UUIDs; no changes made")
    mappings = {item.get("proxmoxSlot"): item for item in inventory.get("proxmoxDataDiskMappings", []) if isinstance(item, dict)}
    expected_slots = {"TB10a": "scsi1", "TB10b": "scsi2", "TB5a": "scsi3", "TB4a": "scsi4", "TB16a": "scsi5"}
    for label, slot in expected_slots.items():
        mapping = mappings.get(slot)
        matched_uuids = [fs.get("uuid") for fs in (mapping or {}).get("filesystemMatches", []) if isinstance(fs, dict)]
        saved_serial = mapping.get("serial") if mapping else None
        saved_slot_serial = disk_serial_from_proxmox(inventory.get("proxmoxVm", {}).get(slot))
        live_slot_serial = disk_serial_from_proxmox(current_proxmox_vm.get(slot))
        if (not mapping or mapping.get("matched") is not True
                or EXPECTED_DATA_FILESYSTEMS[label] not in matched_uuids
                or saved_serial != EXPECTED_PROXMOX_SERIALS[slot]
                or saved_slot_serial != EXPECTED_PROXMOX_SERIALS[slot]
                or live_slot_serial != EXPECTED_PROXMOX_SERIALS[slot]):
            raise RuntimeError(f"Saved inventory does not prove {label} UUID is on Proxmox {slot}; no changes made")

    found = rpc.call("FileSystemMgmt", "enumerateFilesystems", {})
    if not isinstance(found, list):
        raise RuntimeError("OMV did not return detected filesystems")
    for label, uuid in EXPECTED_DATA_FILESYSTEMS.items():
        matches = [item for item in found if isinstance(item, dict) and filesystem_identity_matches(item, uuid)]
        if len(matches) != 1 or matches[0].get("label") != label or matches[0].get("type") != "btrfs":
            raise RuntimeError(f"Expected exactly one existing BTRFS filesystem {label} ({uuid}); detected {len(matches)} matching UUIDs or label/type differs. No changes made")
    mountpoints = config_list(rpc, "conf.system.filesystem.mountpoint", "dir")
    mounts_to_register = []
    for label, uuid in EXPECTED_DATA_FILESYSTEMS.items():
        candidates = [item for item in found if isinstance(item, dict) and filesystem_identity_matches(item, uuid)]
        registered = [mount for mount in mountpoints if isinstance(mount, dict) and mount_config_matches_filesystem(mount, candidates[0])]
        if len(registered) > 1:
            raise RuntimeError(f"Multiple OMV mount records reference {label} ({uuid}); no changes made")
        if not registered:
            mounts_to_register.append({"label": label, "uuid": uuid, "id": uuid})

    shared_folders = inventory.get("sharedFolders", [])
    required_names = {"TB10a", "TB10b", "TB5a", "TB4a", "TB16a", "Kubernetes"}
    if not isinstance(shared_folders, list) or not required_names.issubset({row.get("name") for row in shared_folders if isinstance(row, dict)}):
        raise RuntimeError("Saved inventory is missing one or more expected shared folders; no changes made")
    nfs = inventory.get("nfsExports", [])
    smb = inventory.get("smbShares", [])
    smb_settings = inventory.get("smbSettings")
    if not isinstance(nfs, list) or not isinstance(smb, list) or not isinstance(smb_settings, dict):
        raise RuntimeError("Saved inventory is missing NFS or SMB state; no changes made")
    accounts = inventory.get("userAccounts", [])
    groups = inventory.get("customGroups", [])
    if not isinstance(accounts, list) or not isinstance(groups, list):
        raise RuntimeError("Saved inventory is missing local account/group data; recapture it")
    users_group = inventory.get("primaryUsersGroup")
    if not isinstance(users_group, dict) or users_group.get("name") != "users" or users_group.get("gid", users_group.get("GID")) is None:
        raise RuntimeError("Saved inventory is missing OMV's default users group GID; recapture it")
    live_accounts = rpc.call("UserMgmt", "enumerateAllUsers", {"detail": "basic"})
    live_uids = {str(user.get("uid", user.get("UID", ""))): user.get("name") for user in live_accounts if isinstance(user, dict)}
    for account in accounts:
        if not isinstance(account, dict) or not account.get("name"):
            raise RuntimeError("Saved inventory contains an invalid local user account")
        uid = str(account.get("uid", account.get("UID", "")))
        saved_gid = str(account.get("gid", account.get("GID", "")))
        users_gid = str(users_group.get("gid", users_group.get("GID", ""))) if isinstance(users_group, dict) else ""
        if saved_gid and users_gid and saved_gid != users_gid:
            raise RuntimeError(f"User {account['name']} has primary GID {saved_gid}, but the restore RPC creates users in the default 'users' group GID {users_gid}; refusing to change file ownership semantics")
        if uid and uid in live_uids and live_uids[uid] != account["name"]:
            raise RuntimeError(f"UID collision for saved OMV user {account['name']}; UID {uid} is already assigned to {live_uids[uid]}")
    live_groups = rpc.call("UserMgmt", "enumerateAllGroups", {})
    live_gids = {str(group.get("gid", group.get("GID", ""))): group.get("name") for group in live_groups if isinstance(group, dict)}
    for group in groups:
        if not isinstance(group, dict) or not group.get("name"):
            raise RuntimeError("Saved inventory contains an invalid local group")
        gid = str(group.get("gid", group.get("GID", "")))
        if gid and gid in live_gids and live_gids[gid] != group["name"]:
            raise RuntimeError(f"GID collision for saved OMV group {group['name']}; GID {gid} is already assigned to {live_gids[gid]}")
    admin_name = read_infisical_secret(token, "OMV_USER")
    for account in accounts:
        name = account["name"]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}\$?", name):
            raise RuntimeError(f"Saved OMV account name {name!r} cannot be mapped safely to an Infisical password secret")
        if name != admin_name:
            read_infisical_secret(token, f"OMV_PASSWORD_{name}", "/omv/user-passwords")

    print("Restore inventory and live Proxmox configuration match all five expected disk serials, SCSI slots, BTRFS UUIDs and labels.", flush=True)
    print("Expected data disks: " + ", ".join(f"{slot}={label}/{EXPECTED_DATA_FILESYSTEMS[label]}" for label, slot in expected_slots.items()), flush=True)
    print(f"Plan: register {len(mounts_to_register)} existing BTRFS filesystem(s); create no filesystem and format no disk.", flush=True)
    print(f"Plan: restore {len(shared_folders)} shared folders, {len(smb)} SMB shares, {len(nfs)} NFS exports and saved SMB service settings.", flush=True)
    print(f"Plan: restore {len(accounts)} non-system user account(s) and {len(groups)} custom group(s). Supply each user's new password as /omv/user-passwords/OMV_PASSWORD_<username> in Infisical.", flush=True)
    print("Plan: leave all network interfaces, addresses, routes and firewall settings unchanged.", flush=True)
    return mounts_to_register


def apply_restore(rpc, inventory, mounts_to_register, infisical_token):
    """Recreate OMV records using supported RPC calls without touching disk data."""
    for mount in mounts_to_register:
        rpc.call("FileSystemMgmt", "setMountPoint", {"id": mount["id"], "comment": f"Restored existing {mount['label']} filesystem"})
        print(f"Registered existing filesystem {mount['label']} ({mount['uuid']}); no format/create operation was called.", flush=True)
    rpc.call("Config", "applyChanges", {"modules": ["fstab"], "force": False})
    print("Applied OMV fstab module to mount existing filesystems.", flush=True)

    # Re-read the actual OMV mount records and bind each expected UUID to its
    # new mount-point reference; never assume that config-object UUIDs survive.
    mounted = rpc.call("FileSystemMgmt", "enumerateMountedFilesystems", {"includeroot": False})
    live_mounts = config_list(rpc, "conf.system.filesystem.mountpoint", "dir")
    uuid_to_mountref = {}
    for item in mounted:
        if not isinstance(item, dict) or item.get("uuid", "").lower() not in {v.lower() for v in EXPECTED_DATA_FILESYSTEMS.values()}:
            continue
        fs_uuid = item["uuid"].lower()
        refs = [mp for mp in live_mounts if isinstance(mp, dict) and mount_config_matches_filesystem(mp, item)]
        if len(refs) != 1:
            raise RuntimeError(f"Could not uniquely resolve OMV mountpoint reference for existing filesystem UUID {fs_uuid}")
        uuid_to_mountref[fs_uuid] = refs[0]["uuid"]
    if len(uuid_to_mountref) != len(EXPECTED_DATA_FILESYSTEMS):
        raise RuntimeError("Not all five existing data filesystems are mounted after registration; stopping before shares")

    source_mount_labels = source_mount_to_label(inventory)
    folders_by_name = {item.get("name"): item for item in inventory["sharedFolders"] if isinstance(item, dict)}
    live_folders = config_list(rpc, "conf.system.sharedfolder", "name")
    live_by_name = {item.get("name"): item for item in live_folders if isinstance(item, dict)}
    for name, old in folders_by_name.items():
        label = source_mount_labels.get(old.get("mntentref"))
        if label not in EXPECTED_DATA_FILESYSTEMS:
            raise RuntimeError(f"Saved shared folder {name} does not map to a verified data filesystem")
        expected_relpath = str(old.get("reldirpath", "")).strip("/")
        if not expected_relpath or ".." in expected_relpath.split("/"):
            raise RuntimeError(f"Saved shared-folder path for {name} is invalid")
        existing = live_by_name.get(name)
        new_mountref = uuid_to_mountref[EXPECTED_DATA_FILESYSTEMS[label].lower()]
        if existing:
            if existing.get("mntentref") != new_mountref or str(existing.get("reldirpath", "")).strip("/") != expected_relpath:
                raise RuntimeError(f"Existing shared folder {name} points somewhere else; refusing to move it")
            new_object = existing
        else:
            payload = {key: old[key] for key in ("comment", "privileges") if key in old}
            payload.update({"name": name, "mntentref": new_mountref, "reldirpath": expected_relpath, "mode": "775"})
            new_object = rpc.call("ShareMgmt", "set", payload)
            print(f"Restored shared-folder definition {name} on {label}/{expected_relpath}.", flush=True)
        live_by_name[name] = new_object

    # Recreate only missing service records; matching records are retained.
    current_nfs = rpc_list(rpc, "NFS", "getShareList", {"start": 0, "limit": -1, "sortfield": "sharedfoldername", "sortdir": "ASC"})
    current_nfs_by_name = {item.get("sharedfoldername"): item for item in current_nfs if isinstance(item, dict)}
    for old in inventory["nfsExports"]:
        folder_name = old.get("sharedfoldername")
        folder = live_by_name.get(folder_name)
        if not folder:
            raise RuntimeError(f"Cannot restore NFS export for missing shared folder {folder_name}")
        payload = {key: value for key, value in old.items() if key not in {"uuid", "sharedfoldername"}}
        payload["sharedfolderref"] = folder["uuid"]
        payload["mntentref"] = folder["mntentref"]
        existing = current_nfs_by_name.get(folder_name)
        if existing:
            payload["uuid"] = existing["uuid"]
        rpc.call("NFS", "setShare", payload)
        print(f"Restored NFS export definition for {folder_name}.", flush=True)
    nfs_settings = inventory.get("nfsSettings")
    if not isinstance(nfs_settings, dict):
        raise RuntimeError("Saved inventory has no NFS service settings")
    if rpc.call("NFS", "getSettings", {}) != nfs_settings:
        rpc.call("NFS", "setSettings", nfs_settings)

    current_smb = rpc_list(rpc, "SMB", "getShareList", {"start": 0, "limit": -1, "sortfield": "sharedfoldername", "sortdir": "ASC"})
    current_smb_by_name = {item.get("sharedfoldername"): item for item in current_smb if isinstance(item, dict)}
    for old in inventory["smbShares"]:
        name = old.get("sharedfoldername")
        folder = live_by_name.get(name)
        if not folder:
            raise RuntimeError(f"Cannot restore SMB share for missing shared folder {name}")
        payload = {key: value for key, value in old.items() if key != "sharedfoldername"}
        payload["sharedfolderref"] = folder["uuid"]
        existing = current_smb_by_name.get(name)
        if existing:
            payload["uuid"] = existing["uuid"]
        rpc.call("SMB", "setShare", payload)
        print(f"Restored SMB share definition for {name}.", flush=True)

    admin_name = read_infisical_secret(infisical_token, "OMV_USER")
    for group in inventory.get("customGroups", []):
        group_payload = {
            "name": group["name"],
            "comment": group.get("comment") or "",
            "members": [],
        }
        gid = group.get("gid", group.get("GID"))
        if gid is not None:
            group_payload["gid"] = gid
        rpc.call("UserMgmt", "setGroup", group_payload)
    for account in inventory.get("userAccounts", []):
        name = account["name"]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}\$?", name):
            raise RuntimeError(f"Saved OMV account name {name!r} cannot be mapped safely to an Infisical password secret")
        if name == admin_name:
            password = read_infisical_secret(infisical_token, "OMV_USER_PASSWORD")
        else:
            password = read_infisical_secret(infisical_token, f"OMV_PASSWORD_{name}", "/omv/user-passwords")
        payload = {
            "name": name,
            "groups": account.get("groups", []),
            "shell": account.get("shell", "/usr/sbin/nologin"),
            "password": password,
            "email": "",
            "comment": account.get("comment", ""),
            "disallowusermod": bool(account.get("disallowusermod", False)),
            "sshpubkeys": account.get("sshpubkeys", []),
        }
        uid = account.get("uid", account.get("UID"))
        if uid is not None:
            payload["uid"] = uid
        rpc.call("UserMgmt", "setUser", payload)
        print(f"Restored OMV account {name} with its saved UID and supplemental groups.", flush=True)

    live_smb_settings = rpc.call("SMB", "getSettings", {})
    if live_smb_settings != inventory["smbSettings"]:
        rpc.call("SMB", "setSettings", inventory["smbSettings"])
    rpc.call("Config", "applyChanges", {"modules": ["sharedfolders", "samba", "nfs"], "force": False})
    print("Applied OMV shared-folder, SMB/Samba, and NFS modules.", flush=True)
    print("Restore complete for existing filesystems, users/groups, SMB and NFS definitions. Network and Proxmox settings were left unchanged; data disks were not formatted.", flush=True)


SENSITIVE_CONFIG_KEYS = {
    "password", "passwd", "crypt", "hash",
    "privatekey", "wpapsk", "secret", "secretvalue", "token", "email",
}


def redact(value):
    """Remove password and key material from nested OMV RPC responses."""
    if isinstance(value, dict):
        safe = {}
        for key, item in value.items():
            normalized = "".join(character for character in key.lower() if character.isalnum())
            if normalized in SENSITIVE_CONFIG_KEYS or any(
                    marker in normalized for marker in ("password", "passwd", "secret", "token", "privatekey", "email")):
                continue
            safe[key] = redact(item)
        return safe
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def disk_serial_from_proxmox(value):
    """Return the drive serial from a Proxmox /dev/disk/by-id passthrough."""
    if not isinstance(value, str) or "/dev/disk/by-id/" not in value:
        return None
    device_id = value.split("/dev/disk/by-id/", 1)[1].split(",", 1)[0]
    return device_id.rsplit("_", 1)[1] if "_" in device_id else None


def map_data_disks(proxmox_vm, disks, filesystems):
    """Match Proxmox passthrough disks to OMV records by serial or SCSI slot."""
    mappings = []
    for slot in ("scsi1", "scsi2", "scsi3", "scsi4", "scsi5"):
        backing = proxmox_vm.get(slot)
        serial = disk_serial_from_proxmox(backing)
        # Proxmox exposes the physical by-id serial to the host, while QEMU
        # presents a virtual disk serial such as drive-scsi1 to OMV. Match
        # this stable virtual slot identity as a fallback.
        guest_serial = f"drive-{slot}"
        matches = []
        for disk in disks:
            if not isinstance(disk, dict):
                continue
            links = disk.get("devicelinks", [])
            candidates = [disk.get("serialnumber", ""), *(links if isinstance(links, list) else [])]
            candidate_values = [str(candidate).casefold() for candidate in candidates]
            physical_match = serial and any(
                serial.casefold() == candidate
                or candidate.endswith("_" + serial.casefold())
                for candidate in candidate_values
            )
            slot_match = any(guest_serial.casefold() in candidate for candidate in candidate_values)
            if physical_match or slot_match:
                matches.append(disk)
        match = matches[0] if len(matches) == 1 else None
        devicefile = match.get("devicefile") if match else None
        device_paths = set()
        if match:
            device_paths.update(
                path for path in (match.get("devicefile"), match.get("canonicaldevicefile"))
                if isinstance(path, str) and path
            )
            devicename = match.get("devicename")
            if isinstance(devicename, str) and devicename:
                device_paths.add(f"/dev/{devicename}")
        related_filesystems = []
        if device_paths:
            related_filesystems = [
                fs for fs in filesystems
                if isinstance(fs, dict)
                and device_paths.intersection((fs.get("devicefile"), fs.get("parentdevicefile")))
            ]
        mappings.append({
            "proxmoxSlot": slot,
            "proxmoxBacking": backing,
            "serial": serial,
            "guestSlotIdentity": guest_serial,
            "omvDevice": devicefile,
            "omvDeviceName": match.get("devicename") if match else None,
            "filesystemMatches": [
                {key: fs.get(key) for key in ("uuid", "label", "type", "devicefile", "parentdevicefile", "mountpoint") if key in fs}
                for fs in related_filesystems
            ],
            "matched": match is not None,
        })
    return mappings


def inventory(rpc, proxmox_vm):
    """Capture non-secret OMV state as a Jenkins artifact for rebuild planning."""
    filesystems = rpc.call("FileSystemMgmt", "enumerateMountedFilesystems", {"includeroot": False})
    if not isinstance(filesystems, list):
        raise RuntimeError("OMV did not return the mounted filesystem inventory")
    disks = rpc.call("DiskMgmt", "enumerateDevices", {})
    if not isinstance(disks, list):
        raise RuntimeError("OMV did not return the physical disk inventory")

    data = {
        "schemaVersion": 3,
        "omvUrl": OMV_URL,
        "proxmoxVm": proxmox_vm,
        "disks": redact(disks),
        "filesystems": redact(filesystems),
        "proxmoxDataDiskMappings": map_data_disks(proxmox_vm, disks, filesystems),
        "mountpoints": redact(config_list(rpc, "conf.system.filesystem.mountpoint", "dir")),
        "sharedFolders": redact(config_list(rpc, "conf.system.sharedfolder", "name")),
        "networkInterfaces": redact(config_list(rpc, "conf.system.network.interface", "devicename")),
        "users": redact(config_list(rpc, "conf.system.usermngmnt.user", "name")),
        "groups": redact(config_list(rpc, "conf.system.usermngmnt.group", "name")),
        "userAccounts": redact(rpc_list(rpc, "UserMgmt", "getUserList", {
            "start": 0, "limit": -1, "sortfield": "name", "sortdir": "ASC", "search": "", "detail": "full",
        })),
        "customGroups": redact(rpc.call("UserMgmt", "enumerateGroups", {})),
        "primaryUsersGroup": redact(rpc.call("UserMgmt", "getGroup", {"name": "users"})),
        "nfsSettings": redact(rpc.call("NFS", "getSettings", {})),
        "nfsExports": redact(rpc_list(rpc, "NFS", "getShareList", {
            "start": 0, "limit": -1, "sortfield": "sharedfoldername", "sortdir": "ASC",
        })),
    }
    # SMB RPC is present in OMV 8. If unavailable, fail visibly instead of
    # presenting an incomplete inventory as a complete rebuild reference.
    data["smbSettings"] = redact(rpc.call("SMB", "getSettings", {}))
    data["smbShares"] = redact(rpc_list(rpc, "SMB", "getShareList", {
        "start": 0, "limit": -1, "sortfield": "sharedfoldername", "sortdir": "ASC",
    }))
    with open(INVENTORY_PATH, "w", encoding="utf-8") as output:
        json.dump(data, output, indent=2, sort_keys=True)
        output.write("\n")
    os.chmod(INVENTORY_PATH, 0o600)
    print(f"Saved OMV configuration inventory to {INVENTORY_PATH} (account contact details, passwords, tokens, and key material excluded).", flush=True)
    print(f"Physical disks: {len(disks)}; mounted filesystems: {len(filesystems)}; shared folders: {len(data['sharedFolders'])}; users: {len(data['users'])}; groups: {len(data['groups'])}; NFS exports: {len(data['nfsExports'])}; SMB shares: {len(data['smbShares'])}.", flush=True)
    print("Proxmox VM hardware inventory was captured alongside the OMV disk and filesystem inventory.", flush=True)
    matched = sum(1 for item in data["proxmoxDataDiskMappings"] if item["matched"])
    print(f"Proxmox physical data disk serial matches: {matched}/{len(data['proxmoxDataDiskMappings'])}.", flush=True)
    if matched != len(data["proxmoxDataDiskMappings"]):
        print("Some Proxmox-to-OMV disk mappings need manual verification before a rebuild; no disk changes were made.", flush=True)
    print("This inventory is a rebuild input; it does not change Proxmox or OMV and does not restore configuration.", flush=True)


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
    if ACTION not in {"plan", "apply", "inventory", "restore-plan", "restore"}:
        raise RuntimeError("OMV_ACTION must be plan, apply, inventory, restore-plan, or restore")
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
    restore_inventory = load_restore_inventory(token) if ACTION in {"restore-plan", "restore"} else None
    username = read_infisical_secret(token, "OMV_USER")
    password = read_infisical_secret(token, "OMV_USER_PASSWORD")
    rpc = OmvRpc(username, password)
    try:
        if ACTION == "inventory":
            inventory(rpc, read_proxmox_vm_config())
            return
        if ACTION == "restore-plan":
            current_proxmox_vm = read_proxmox_vm_config()
            restore_plan(rpc, restore_inventory, token, current_proxmox_vm)
            print("Read-only restore preflight complete. Check disk identity and network reachability before selecting APPLY.", flush=True)
            return
        if ACTION == "restore":
            current_proxmox_vm = read_proxmox_vm_config()
            mounts = restore_plan(rpc, restore_inventory, token, current_proxmox_vm)
            apply_restore(rpc, restore_inventory, mounts, token)
            return
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
