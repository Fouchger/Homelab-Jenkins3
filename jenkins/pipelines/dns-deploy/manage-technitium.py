#!/usr/bin/env python3
"""Run within a DNS LXC to initialize its admin password and manage zone replication."""

import json
import base64
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:5380"


def call(path, form=None, token=None, allow_error=False):
    headers = {"Accept": "application/json"}
    body = None
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.loads(response.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        if allow_error:
            return None
        raise RuntimeError("Technitium API is not ready or returned invalid data") from None
    if result.get("status") not in ("ok",) and not allow_error:
        raise RuntimeError("Technitium API rejected a configuration request")
    return result


def login(password):
    result = call("/api/user/login", {"user": "admin", "pass": password, "includeInfo": "true"}, allow_error=True)
    if isinstance(result, dict) and result.get("status") == "ok" and result.get("token"):
        return result["token"]
    return None


def authenticate(desired_password):
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        status = call("/api/status", allow_error=True)
        if isinstance(status, dict) and status.get("status") == "ok":
            break
        time.sleep(5)
    else:
        raise RuntimeError("Technitium did not become ready within 10 minutes")

    token = login(desired_password)
    if token:
        return token
    token = login("admin")
    if not token:
        raise RuntimeError("Cannot authenticate with the saved DNS admin password or the new-install default; refusing to reset credentials")
    changed = call("/api/user/changePassword", {"pass": "admin", "newPass": desired_password}, token=token)
    if changed.get("status") != "ok":
        raise RuntimeError("Could not set the supplied Technitium admin password")
    token = login(desired_password)
    if not token:
        raise RuntimeError("Technitium admin password change could not be verified")
    return token


def zones(token):
    return primary_zones(token, include_internal=False)


def primary_zones(token, include_internal):
    result = call("/api/zones/list", token=token)
    entries = result.get("response", {}).get("zones", [])
    if not isinstance(entries, list):
        raise RuntimeError("Technitium returned an invalid zone list")
    return [
        item["name"] for item in entries
        if isinstance(item, dict) and item.get("type") == "Primary"
        and (include_internal or not item.get("internal", False)) and item.get("name")
    ]


def all_zones(token):
    result = call("/api/zones/list", token=token)
    entries = result.get("response", {}).get("zones", [])
    if not isinstance(entries, list):
        raise RuntimeError("Technitium returned an invalid zone list")
    return [item for item in entries if isinstance(item, dict) and item.get("name")]


def export_zone(token, zone):
    query = urllib.parse.urlencode({"zone": zone})
    request = urllib.request.Request(
        BASE + "/api/zones/export?" + query,
        headers={"Accept": "text/plain", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            content = response.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, UnicodeDecodeError):
        raise RuntimeError(f"Could not export Technitium zone {zone}") from None
    if not content.strip():
        raise RuntimeError(f"Technitium returned an empty export for zone {zone}")
    return content


def import_zone(token, zone, content):
    query = urllib.parse.urlencode({
        "zone": zone,
        "overwriteZone": "true",
        "overwriteSoaSerial": "false",
    })
    request = urllib.request.Request(
        BASE + "/api/zones/import?" + query,
        data=content.encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "text/plain",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode())
    except (urllib.error.URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError(f"Could not import preserved records for zone {zone}") from None
    if not isinstance(result, dict) or result.get("status") != "ok":
        raise RuntimeError(f"Technitium rejected the preserved zone data for {zone}")


def verify_dns_listener():
    """Require Technitium's local DNS listener to return a valid DNS response."""
    transaction_id = os.urandom(2)
    # Query the root NS record over loopback so this health check does not
    # depend on WAN connectivity or a particular hosted zone being present.
    header = transaction_id + b"\x01\x00\x00\x01" + b"\x00" * 6
    query = header + b"\x00" + b"\x00\x02\x00\x01"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as dns_socket:
            dns_socket.settimeout(5)
            dns_socket.sendto(query, ("127.0.0.1", 53))
            response, _peer = dns_socket.recvfrom(4096)
    except OSError:
        raise RuntimeError("Technitium DNS listener did not answer a UDP query on 127.0.0.1:53") from None
    if len(response) < 12 or response[:2] != transaction_id or not (response[2] & 0x80):
        raise RuntimeError("Technitium DNS listener returned an invalid DNS response")


def main():
    if len(sys.argv) != 2:
        raise RuntimeError("Expected one JSON configuration file")
    with open(sys.argv[1], encoding="utf-8") as handle:
        config = json.load(handle)
    password = config.get("admin_password", "")
    if not isinstance(password, str) or len(password) < 6 or any(ch in password for ch in "\r\n\0"):
        raise RuntimeError("Technitium admin password is missing or invalid")
    token = authenticate(password)
    mode = config.get("action")
    if mode == "verify":
        verify_dns_listener()
        print("Technitium admin authentication and DNS listener verified.")
        return
    if mode == "list-zones":
        print(json.dumps(zones(token)))
        return
    if mode == "export-zones":
        zone_types = config.get("zone_types", ["Primary"])
        if not isinstance(zone_types, list) or not all(item in ("Primary", "Secondary") for item in zone_types):
            raise RuntimeError("Zone export types are invalid")
        zone_files = {}
        for item in all_zones(token):
            if item.get("type") in zone_types:
                zone_files[item["name"]] = base64.b64encode(export_zone(token, item["name"]).encode("utf-8")).decode("ascii")
        print(json.dumps(zone_files, separators=(",", ":")))
        return
    if mode == "import-zones":
        zone_files = config.get("zone_files")
        if not isinstance(zone_files, dict):
            raise RuntimeError("Preserved zone data is invalid")
        current = call("/api/zones/list", token=token).get("response", {}).get("zones", [])
        by_name = {item.get("name"): item for item in current if isinstance(item, dict) and item.get("name")}
        for zone, encoded in zone_files.items():
            if not isinstance(zone, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", zone) or not isinstance(encoded, str):
                raise RuntimeError("Preserved zone data contains an invalid zone name or payload")
            existing = by_name.get(zone)
            if existing and existing.get("type") != "Primary":
                raise RuntimeError(f"Cannot restore {zone}: it exists as a non-primary zone on dns01")
            if not existing:
                call("/api/zones/create", {"zone": zone, "type": "Primary"}, token=token)
            try:
                content = base64.b64decode(encoded, validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                raise RuntimeError(f"Preserved zone data for {zone} is invalid") from None
            import_zone(token, zone, content)
        print(f"Restored {len(zone_files)} preserved primary zone(s).")
        return
    if mode == "ensure-primary-zones":
        current = call("/api/zones/list", token=token).get("response", {}).get("zones", [])
        by_name = {item.get("name"): item for item in current if isinstance(item, dict)}
        for zone in config.get("zones", []):
            existing = by_name.get(zone)
            if existing and existing.get("type") != "Primary":
                raise RuntimeError(f"Zone {zone} already exists as a non-primary zone on dns01; refusing to replace it")
            if not existing:
                call("/api/zones/create", {"zone": zone, "type": "Primary"}, token=token)
        print(f"Ensured {len(config.get('zones', []))} configured primary zone(s).")
        return
    if mode == "allow-transfers":
        secondary_ip = config["secondary_ipv4"]
        for zone in config.get("zones", []):
            call("/api/zones/options/set", {
                "zone": zone,
                "zoneTransfer": "UseSpecifiedNetworkACL",
                "zoneTransferNetworkACL": secondary_ip,
                "notify": "SpecifiedNameServers",
                "notifyNameServers": secondary_ip,
            }, token=token)
        print(f"Enabled restricted zone transfers for {len(config.get('zones', []))} primary zone(s).")
        return
    if mode == "configure-secondaries":
        primary_ip = config["primary_ipv4"]
        current_result = call("/api/zones/list", token=token)
        current = {item.get("name"): item for item in current_result.get("response", {}).get("zones", []) if isinstance(item, dict)}
        for zone in config.get("zones", []):
            existing = current.get(zone)
            if existing and existing.get("type") != "Secondary":
                raise RuntimeError(f"Zone {zone} exists on dns02 as a non-secondary zone; refusing to replace it")
            if not existing:
                call("/api/zones/create", {
                    "zone": zone,
                    "type": "Secondary",
                    "primaryNameServerAddresses": primary_ip,
                    "zoneTransferProtocol": "Tcp",
                }, token=token)
            else:
                call("/api/zones/options/set", {
                    "zone": zone,
                    "primaryNameServerAddresses": primary_ip,
                    "primaryZoneTransferProtocol": "Tcp",
                }, token=token)
                call("/api/zones/resync", {"zone": zone}, token=token)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            result = call("/api/zones/list", token=token)
            replicas = {item.get("name"): item for item in result.get("response", {}).get("zones", []) if isinstance(item, dict)}
            if all(zone in replicas and replicas[zone].get("type") == "Secondary" and not replicas[zone].get("syncFailed", False) for zone in config.get("zones", [])):
                break
            time.sleep(5)
        else:
            raise RuntimeError("One or more secondary zones did not complete a successful transfer")
        print(f"Configured or resynced {len(config.get('zones', []))} secondary zone(s).")
        return
    raise RuntimeError("Unknown Technitium configuration action")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
