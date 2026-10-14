#!/usr/bin/env python3
"""Run OpenTofu with Proxmox API credentials read from Infisical."""

import json
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import quote


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required setting {name} is missing")
    return value


def request_json(url, method="GET", token=None, form=None):
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
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Infisical request failed with status {exc.code}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Infisical request failed: {type(exc).__name__}") from None
    try:
        result = json.loads(payload.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("Infisical returned invalid JSON") from None
    if not isinstance(result, dict):
        raise RuntimeError("Infisical returned an unexpected response")
    return result


def login(base_url):
    response = request_json(
        f"{base_url}/api/v1/auth/universal-auth/login",
        method="POST",
        form={
            "clientId": required("INFISICAL_READ_CLIENT_ID"),
            "clientSecret": required("INFISICAL_READ_CLIENT_SECRET"),
        },
    )
    token = response.get("accessToken")
    if not isinstance(token, str) or not token:
        raise RuntimeError("Infisical did not return a read access token")
    return token


def read_secret(base_url, token, name):
    query = urllib.parse.urlencode({
        "projectId": required("INFISICAL_PROJECT_ID"),
        "environment": required("INFISICAL_ENVIRONMENT"),
        "secretPath": "/proxmox/automation",
    })
    url = f"{base_url}/api/v4/secrets/{urllib.parse.quote(name)}?{query}"
    result = request_json(url, token=token)
    secret = result.get("secret", {})
    value = secret.get("secretValue") if isinstance(secret, dict) else None
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Infisical secret {name} is missing or unreadable")
    return value.strip()


def verify_proxmox_tls(host):
    url = f"https://{host}:8006/api2/json/version"
    try:
        with urllib.request.urlopen(url, timeout=10):
            pass
    except urllib.error.HTTPError as exc:
        # Proxmox normally requires authentication for this endpoint. Receiving
        # an HTTP response proves the TLS handshake and hostname check passed.
        if exc.code not in {401, 403}:
            raise RuntimeError(
                f"Proxmox API TLS preflight reached the endpoint but returned HTTP {exc.code}"
            ) from None
    except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise RuntimeError(
            "Proxmox API TLS preflight failed "
            f"({type(reason).__name__}). Set PROXMOX_API_HOSTNAME to a DNS name present in "
            "the certificate SAN; Job 002 manages the certificate for pve01.fouchger.uk."
        ) from None


def proxmox_request(host, token_id, token_secret, path, method="GET", form=None):
    url = f"https://{host}:8006/api2/json/{path.lstrip('/')}"
    headers = {
        "Accept": "application/json",
        "Authorization": f"PVEAPIToken={token_id}={token_secret}",
    }
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Proxmox protection request failed with HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as exc:
        raise RuntimeError(f"Proxmox protection request failed ({type(exc).__name__})") from None
    try:
        result = json.loads(payload.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("Proxmox returned invalid JSON while changing VM protection") from None
    if not isinstance(result, dict):
        raise RuntimeError("Proxmox returned an unexpected response while changing VM protection")
    return result.get("data")


def inspect_plan(environment_dir, plan_file, recreate_node):
    result = subprocess.run(
        ["tofu", "-chdir=" + str(environment_dir), "show", "-json", str(plan_file)],
        check=True, capture_output=True, text=True,
    )
    try:
        plan = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("OpenTofu returned invalid plan JSON") from None
    changes = [
        change for change in plan.get("resource_changes", [])
        if change.get("type") == "proxmox_virtual_environment_vm"
        and change.get("name") == "talos_node"
    ]
    destructive = [
        change for change in changes
        if "delete" in change.get("change", {}).get("actions", [])
    ]
    if not recreate_node:
        if destructive:
            addresses = ", ".join(change.get("address", "unknown") for change in destructive)
            raise RuntimeError(
                "Plan contains VM deletion/replacement without RECREATE_NODE; "
                f"select and review one node at a time. Affected resources: {addresses}"
            )
        existing_updates = [
            change for change in changes
            if change.get("change", {}).get("before") is not None
            and change.get("change", {}).get("actions") not in (["no-op"], ["read"])
        ]
        if len(existing_updates) > 1:
            addresses = ", ".join(change.get("address", "unknown") for change in existing_updates)
            raise RuntimeError(
                "Plan updates multiple existing Talos VMs without RECREATE_NODE; "
                f"select one node per build. Affected resources: {addresses}"
            )
        return None

    if not re.fullmatch(r"talos-(?:cp-0[1-3]|worker-0[1-2])", recreate_node):
        raise RuntimeError("RECREATE_NODE must be NONE or one of the five configured Talos node names")
    expected = f'proxmox_virtual_environment_vm.talos_node["{recreate_node}"]'
    unexpected_changes = [
        change for change in changes
        if change.get("address") != expected
        and change.get("change", {}).get("actions") != ["no-op"]
    ]
    if unexpected_changes:
        addresses = ", ".join(change.get("address", "unknown") for change in unexpected_changes)
        raise RuntimeError(
            f"A single-node recreation may change only {expected}; other planned VM changes: {addresses}"
        )
    if len(destructive) != 1 or destructive[0].get("address") != expected:
        addresses = ", ".join(change.get("address", "unknown") for change in destructive) or "none"
        raise RuntimeError(
            f"A single-node recreation must replace only {expected}; planned destructive resources: {addresses}"
        )
    actions = destructive[0].get("change", {}).get("actions", [])
    if set(actions) != {"delete", "create"}:
        raise RuntimeError(f"{expected} is not a replacement plan")
    before = destructive[0].get("change", {}).get("before") or {}
    node_name = before.get("node_name")
    vm_id = before.get("vm_id")
    if not isinstance(node_name, str) or not isinstance(vm_id, int):
        raise RuntimeError(f"Could not safely identify the existing Proxmox VM for {expected}")
    return node_name, vm_id


def set_vm_protection(host, token_id, token_secret, node_name, vm_id, enabled):
    path = f"nodes/{quote(node_name, safe='')}/qemu/{vm_id}/config"
    proxmox_request(host, token_id, token_secret, path, method="PUT", form={"protection": "1" if enabled else "0"})


def main():
    action = required("TOFU_ACTION")
    if action not in {"plan", "apply"}:
        raise RuntimeError("TOFU_ACTION must be plan or apply")

    project_root = Path(__file__).resolve().parents[3]
    environment_dir = project_root / "tofu" / "environments" / "kubernetes"
    tfvars_file = Path(required("HOMELAB_KUBERNETES_TFVARS"))
    plan_file = Path(required("TOFU_PLAN_FILE"))
    workspace = Path(required("WORKSPACE")).resolve()
    plan_file = plan_file.resolve()
    if workspace not in plan_file.parents:
        raise RuntimeError("The OpenTofu plan file must remain inside the Jenkins workspace")
    tfvars_file = tfvars_file.resolve()
    if workspace not in tfvars_file.parents or not tfvars_file.is_file():
        raise RuntimeError("The Jenkins cluster configuration file must exist inside the current workspace")
    os.chmod(tfvars_file, 0o600)

    state_dir = Path(required("HOMELAB_KUBERNETES_STATE_DIR"))
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_dir, 0o700)
    state_file = state_dir / "terraform.tfstate"

    host = required("PROXMOX_API_HOSTNAME")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        raise RuntimeError("PROXMOX_API_HOSTNAME must be a DNS hostname or IPv4 address without a scheme")
    verify_proxmox_tls(host)

    infisical_url = required("INFISICAL_URL").rstrip("/")
    read_token = login(infisical_url)
    token_id = read_secret(infisical_url, read_token, "PVE_API_TOKEN_ID")
    token_secret = read_secret(infisical_url, read_token, "PVE_API_TOKEN_SECRET")
    if "!" not in token_id:
        raise RuntimeError("Infisical PVE_API_TOKEN_ID is not a fully qualified Proxmox token ID")

    env = os.environ.copy()
    env["PROXMOX_VE_ENDPOINT"] = f"https://{host}:8006/"
    env["PROXMOX_VE_API_TOKEN"] = f"{token_id}={token_secret}"

    subprocess.run([
        "tofu", "-chdir=" + str(environment_dir), "init", "-input=false",
        "-backend-config=path=" + str(state_file),
    ], env=env, check=True)
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    if action == "plan":
        recreate_node = os.environ.get("RECREATE_NODE", "NONE").strip()
        command = [
            "tofu", "-chdir=" + str(environment_dir), "plan", "-input=false",
            "-var-file=" + str(tfvars_file), "-out=" + str(plan_file),
        ]
        if recreate_node != "NONE":
            address = f'proxmox_virtual_environment_vm.talos_node["{recreate_node}"]'
            command.extend(["-target=" + address, "-replace=" + address])
        subprocess.run(command, env=env, check=True)
        inspect_plan(environment_dir, plan_file, "" if recreate_node == "NONE" else recreate_node)
        return
    else:
        if not plan_file.is_file():
            raise RuntimeError("The approved build plan is missing from this Jenkins workspace")
        command = ["tofu", "-chdir=" + str(environment_dir), "apply", "-input=false", str(plan_file)]
        recreate_node = os.environ.get("RECREATE_NODE", "NONE").strip()
        target = inspect_plan(environment_dir, plan_file, "" if recreate_node == "NONE" else recreate_node)
        if target:
            node_name, vm_id = target
            print(f"Temporarily disabling Proxmox protection for selected VM {vm_id} on {node_name}.")
            set_vm_protection(host, token_id, token_secret, node_name, vm_id, False)
            try:
                subprocess.run(command, env=env, check=True)
            finally:
                try:
                    set_vm_protection(host, token_id, token_secret, node_name, vm_id, True)
                    print(f"Proxmox protection restored for VM {vm_id} on {node_name}.")
                except RuntimeError as exc:
                    print(f"WARNING: could not restore Proxmox protection for VM {vm_id} on {node_name}: {exc}", file=sys.stderr)
                    raise
            return
    subprocess.run(command, env=env, check=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        message = str(exc) if isinstance(exc, RuntimeError) else f"OpenTofu execution failed ({type(exc).__name__})"
        print(message, file=sys.stderr)
        sys.exit(1)
