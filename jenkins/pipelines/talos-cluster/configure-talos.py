#!/usr/bin/env python3
"""Initialize a fresh Talos cluster once, or health-check an existing one."""

import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


SECRET_PATH = "/kubernetes/talos"
SECRETS_NAME = "TALOS_SECRETS"
KUBECONFIG_NAME = "KUBECONFIG"
INITIALIZED_NAME = "TALOS_CLUSTER_INITIALIZED"
INVENTORY = Path("kubernetes/talos/cluster.json")
WORKDIR = Path(os.environ.get("WORKSPACE", ".")) / ".talos-private"


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required setting {name} is missing")
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
        raise RuntimeError(f"Infisical request failed with HTTP {exc.code}; response suppressed") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Infisical request failed ({type(exc).__name__})") from None
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("Infisical returned invalid JSON") from None


def api_base():
    return required("INFISICAL_URL").rstrip("/")


def login(identity):
    token_response = request_json(
        f"{api_base()}/api/v1/auth/universal-auth/login",
        method="POST",
        form={
            "clientId": required(f"INFISICAL_{identity}_CLIENT_ID"),
            "clientSecret": required(f"INFISICAL_{identity}_CLIENT_SECRET"),
        },
    )
    token = token_response.get("accessToken") if isinstance(token_response, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError(f"Infisical {identity.lower()} identity authentication failed")
    return token


def secret_url(name):
    query = urllib.parse.urlencode({
        "projectId": required("INFISICAL_PROJECT_ID"),
        "environment": required("INFISICAL_ENVIRONMENT"),
        "secretPath": SECRET_PATH,
    })
    return f"{api_base()}/api/v4/secrets/{urllib.parse.quote(name)}?{query}"


def get_secret(token, name):
    result = request_json(secret_url(name), token=token, allow_404=True)
    if result is None:
        return None
    secret = result.get("secret") if isinstance(result, dict) else None
    if not isinstance(secret, dict):
        raise RuntimeError(f"Infisical returned an invalid response while checking {name}")
    return secret


def secret_value(secret):
    value = secret.get("secretValue") if isinstance(secret, dict) else None
    return value if isinstance(value, str) and value else None


def read_secret(token, name):
    return secret_value(get_secret(token, name))


def write_secret(token, name, value, *, existed):
    body = {
        "projectId": required("INFISICAL_PROJECT_ID"),
        "environment": required("INFISICAL_ENVIRONMENT"),
        "secretPath": SECRET_PATH,
        "secretValue": value,
        "type": "shared",
        "skipMultilineEncoding": True,
    }
    method = "PATCH" if existed else "POST"
    try:
        request_json(secret_url(name), method=method, token=token, body=body)
    except RuntimeError as exc:
        if "HTTP 404" in str(exc):
            raise RuntimeError(
                f"Infisical could not write {name} in {SECRET_PATH} (HTTP 404). "
                "Check the configured project ID and environment slug, and confirm "
                "the Jenkins writer identity can create or update secrets there."
            ) from None
        raise


def load_inventory():
    data = json.loads(INVENTORY.read_text(encoding="utf-8"))
    vip = ipaddress.ip_address(data["api_vip"])
    nodes = data["nodes"]
    names = [node["name"] for node in nodes]
    addresses = [ipaddress.ip_address(node["ip"]) for node in nodes]
    roles = [node["role"] for node in nodes]
    if len(nodes) != 5 or len(set(names)) != 5 or len(set(addresses)) != 5:
        raise RuntimeError("Inventory must contain five nodes with unique names and addresses")
    if addresses.count(vip) or roles.count("controlplane") != 3 or roles.count("worker") != 2:
        raise RuntimeError("Inventory must define three control planes, two workers, and a separate API VIP")
    if any(role not in ("controlplane", "worker") for role in roles):
        raise RuntimeError("Inventory contains an unsupported Talos node role")
    if not data.get("cluster_name") or not data.get("install_disk"):
        raise RuntimeError("Inventory is missing cluster_name or install_disk")
    return data


def run(args, *, check=True, capture=False, env=None, timeout=None):
    result = subprocess.run(
        args,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        env=env,
        timeout=timeout,
    )
    if check and result.returncode:
        details = result.stdout.strip() if capture and result.stdout else ""
        raise RuntimeError(f"Command failed ({Path(args[0]).name}, exit {result.returncode})" + (f": {details[-1200:]}" if details else ""))
    return result


def tcp_open(host, port):
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def assert_fresh_maintenance(inventory):
    vip = inventory["api_vip"]
    if tcp_open(vip, 6443):
        raise RuntimeError("Kubernetes API VIP already accepts connections; refusing initial bootstrap")
    for node in inventory["nodes"]:
        if not tcp_open(node["ip"], 50000):
            raise RuntimeError(f"{node['name']} is not reachable on Talos maintenance API TCP/50000")
        result = run([
            "talosctl", "get", "disks", "--insecure", "--nodes", node["ip"], "-o", "yaml",
        ], check=False, capture=True)
        if result.returncode or inventory["install_disk"] not in (result.stdout or ""):
            raise RuntimeError(f"{node['name']} is not confirmed in maintenance mode with install disk {inventory['install_disk']}")
    print("Preflight confirms the API VIP is unused and all five nodes expose the expected maintenance-mode install disk.")


def talos_environment(talosconfig):
    env = os.environ.copy()
    env["TALOSCONFIG"] = str(talosconfig)
    return env


def existing_cluster_health(inventory, talosconfig):
    env = talos_environment(talosconfig)
    controlplanes = [node["ip"] for node in inventory["nodes"] if node["role"] == "controlplane"]
    run(["talosctl", "config", "endpoint", *controlplanes], env=env)
    for address in controlplanes:
        result = run(["talosctl", "version", "--nodes", address], check=False, capture=True, env=env)
        if result.returncode:
            details = " ".join((result.stdout or "").split())
            details = details[-600:] if details else f"talosctl exited {result.returncode} without diagnostic output"
            raise RuntimeError(
                f"Authenticated Talos API check failed for control plane {address}: {details}. "
                "Refusing to reapply machine config or bootstrap; investigate the existing/partial cluster."
            )
    print("Saved Talos credentials authenticate to all control planes. No machine config or bootstrap operation will be repeated.")


def prepare_machineconfigs(inventory, secrets_file, output_dir):
    cluster_patch = WORKDIR / "cluster.patch.yaml"
    cluster_patch.write_text(
        "cluster:\n"
        "  network:\n"
        "    cni:\n"
        "      name: none\n"
        "  proxy:\n"
        "    disabled: true\n",
        encoding="utf-8",
    )
    patch_file = WORKDIR / "controlplane.patch.yaml"
    patch_file.write_text(
        "machine:\n"
        "  network:\n"
        "    interfaces:\n"
        "      - deviceSelector:\n"
        "          physical: true\n"
        "        dhcp: true\n"
        "        vip:\n"
        f"          ip: {inventory['api_vip']}\n",
        encoding="utf-8",
    )
    run([
        "talosctl", "gen", "config", inventory["cluster_name"],
        f"https://{inventory['api_vip']}:6443",
        "--install-disk", inventory["install_disk"],
        "--install-image", f"ghcr.io/siderolabs/installer:{inventory['talos_version']}",
        "--kubernetes-version", inventory["kubernetes_version"],
        "--with-secrets", str(secrets_file),
        "--config-patch", f"@{cluster_patch}",
        "--config-patch-control-plane", f"@{patch_file}",
        "--output", str(output_dir),
    ])


def initialize_cluster(inventory, read_token, *, secrets_existed):
    assert_fresh_maintenance(inventory)
    print("Generating persistent Talos machine secrets and saving them to Infisical before touching any node.")
    secrets_file = WORKDIR / "secrets.yaml"
    run(["talosctl", "gen", "secrets", "--output-file", str(secrets_file)])
    os.chmod(secrets_file, 0o600)
    secrets_text = secrets_file.read_text(encoding="utf-8")
    writer = login("WRITE")
    write_secret(writer, SECRETS_NAME, secrets_text, existed=secrets_existed)
    saved = read_secret(read_token, SECRETS_NAME)
    if saved != secrets_text:
        raise RuntimeError("Infisical did not verify the saved Talos machine secrets; nodes were not configured")

    install_cluster_from_secrets(inventory, secrets_file, read_token)


def install_cluster_from_secrets(inventory, secrets_file, read_token):
    """Apply a stored secrets bundle to a fully fresh set of nodes and bootstrap once."""

    configs = WORKDIR / "machineconfig"
    configs.mkdir(mode=0o700)
    prepare_machineconfigs(inventory, secrets_file, configs)

    # Validate the full inventory before sending any config, then apply control planes and workers.
    for node in inventory["nodes"]:
        run(["talosctl", "get", "disks", "--insecure", "--nodes", node["ip"], "-o", "yaml"])
    for role, filename in (("controlplane", "controlplane.yaml"), ("worker", "worker.yaml")):
        for node in inventory["nodes"]:
            if node["role"] == role:
                run([
                    "talosctl", "apply-config", "--insecure", "--nodes", node["ip"],
                    "--file", str(configs / filename),
                ])
                print(f"Talos machine configuration submitted to {node['name']}.")

    controlplanes = [node["ip"] for node in inventory["nodes"] if node["role"] == "controlplane"]
    env = talos_environment(configs / "talosconfig")
    run(["talosctl", "config", "endpoint", *controlplanes], env=env)
    run(["talosctl", "config", "node", controlplanes[0]], env=env)

    # Wait at most 15 minutes total for every configured node to install Talos.
    pending = {node["name"]: node for node in inventory["nodes"]}
    deadline = time.monotonic() + 15 * 60
    while pending and time.monotonic() < deadline:
        for node in list(pending.values()):
            check = run(["talosctl", "version", "--nodes", node["ip"]], check=False, capture=True, env=env)
            if check.returncode == 0:
                print(f"Talos API is available on {node['name']}.")
                del pending[node["name"]]
        if pending:
            time.sleep(15)
    if pending:
        names = ", ".join(pending)
        raise RuntimeError(f"Talos API did not return on {names}; bootstrap was not attempted")

    # Persist the one-time bootstrap marker before calling bootstrap. If the
    # agent loses contact during that call, a rerun must never issue it twice.
    existing_marker_entry = get_secret(read_token, INITIALIZED_NAME)
    existing_marker = secret_value(existing_marker_entry)
    if existing_marker not in (None, inventory["cluster_name"]):
        raise RuntimeError("Infisical initialization marker belongs to a different cluster name")
    writer = login("WRITE")
    write_secret(
        writer, INITIALIZED_NAME, inventory["cluster_name"],
        existed=existing_marker_entry is not None,
    )
    if read_secret(read_token, INITIALIZED_NAME) != inventory["cluster_name"]:
        raise RuntimeError("Infisical did not verify the bootstrap marker; bootstrap was not attempted")

    print("Starting the single initial etcd bootstrap on talos-cp-01.")
    run(["talosctl", "bootstrap", "--nodes", controlplanes[0], "--endpoints", controlplanes[0]], env=env)
    configure_cilium(inventory, env, read_token)


def configure_cilium(inventory, env, read_token):
    controlplanes = [node["ip"] for node in inventory["nodes"] if node["role"] == "controlplane"]
    kubeconfig = WORKDIR / "kubeconfig"
    run(["talosctl", "kubeconfig", str(kubeconfig), "--nodes", controlplanes[0]], env=env)
    os.chmod(kubeconfig, 0o600)
    kube_env = env.copy()
    kube_env["KUBECONFIG"] = str(kubeconfig)
    run([
        "helm", "upgrade", "--install", "cilium", "oci://quay.io/cilium/charts/cilium",
        "--version", inventory["cilium_version"], "--namespace", "kube-system", "--create-namespace",
        "--set", "ipam.mode=kubernetes",
        "--set", "kubeProxyReplacement=true",
        "--set", "securityContext.capabilities.ciliumAgent={CHOWN,KILL,NET_ADMIN,NET_RAW,IPC_LOCK,SYS_ADMIN,SYS_RESOURCE,DAC_OVERRIDE,FOWNER,SETGID,SETUID}",
        "--set", "securityContext.capabilities.cleanCiliumState={NET_ADMIN,SYS_ADMIN,SYS_RESOURCE}",
        "--set", "cgroup.autoMount.enabled=false",
        "--set", "cgroup.hostRoot=/sys/fs/cgroup",
        "--set", "k8sServiceHost=localhost",
        "--set", "k8sServicePort=7445",
        "--wait", "--timeout", "15m",
    ], env=kube_env)
    deadline = time.monotonic() + 10 * 60
    expected_ips = {node["ip"] for node in inventory["nodes"]}
    ready_ips = set()
    last_node_summary = "Kubernetes has not returned node status yet."
    while time.monotonic() < deadline:
        remaining = max(1, int(deadline - time.monotonic()))
        try:
            result = run(
                ["kubectl", "get", "nodes", "-o", "json"],
                check=False,
                capture=True,
                env=kube_env,
                timeout=min(20, remaining),
            )
        except subprocess.TimeoutExpired:
            print("kubectl get nodes timed out; retrying while checking cluster readiness.", flush=True)
            time.sleep(min(15, max(0, deadline - time.monotonic())))
            continue

        if result.returncode == 0:
            try:
                payload = json.loads(result.stdout)
                ready_ips = set()
                node_summaries = []
                for item in payload.get("items", []):
                    name = item.get("metadata", {}).get("name", "unknown")
                    status = item.get("status", {})
                    conditions = status.get("conditions", [])
                    ready_condition = next(
                        (condition for condition in conditions if condition.get("type") == "Ready"),
                        {},
                    )
                    is_ready = ready_condition.get("status") == "True"
                    internal_ips = {
                        address.get("address")
                        for address in status.get("addresses", [])
                        if address.get("type") == "InternalIP"
                    }
                    if is_ready:
                        ready_ips.update(internal_ips & expected_ips)
                    if not is_ready:
                        reason = ready_condition.get("reason", "Ready condition missing")
                        message = ready_condition.get("message", "").strip()
                        node_summaries.append(f"{name} ({','.join(sorted(internal_ips)) or 'no InternalIP'}): {reason} {message}".strip())
                    else:
                        node_summaries.append(f"{name} ({','.join(sorted(internal_ips)) or 'no InternalIP'}): Ready")
                last_node_summary = "; ".join(node_summaries) or "Kubernetes returned no nodes."
                pending_ips = sorted(expected_ips - ready_ips)
                print(
                    f"Kubernetes node readiness: {len(ready_ips)}/{len(expected_ips)} expected addresses Ready; "
                    f"pending {', '.join(pending_ips) if pending_ips else 'none'}. "
                    f"Observed: {last_node_summary}",
                    flush=True,
                )
                if expected_ips.issubset(ready_ips):
                    break
            except (json.JSONDecodeError, KeyError, TypeError):
                last_node_summary = "kubectl returned invalid node status JSON."
                print(f"{last_node_summary} Retrying.", flush=True)
        else:
            details = (result.stdout or "").strip().replace("\n", " ")
            last_node_summary = details[-500:] or f"kubectl exited {result.returncode} without diagnostic output."
            print(
                f"kubectl get nodes failed (exit {result.returncode}); "
                f"retrying. {last_node_summary}",
                flush=True,
            )
        time.sleep(min(15, max(0, deadline - time.monotonic())))
    else:
        pending_ips = ", ".join(sorted(expected_ips - ready_ips)) or "unknown"
        raise RuntimeError(
            f"Cilium was installed, but Kubernetes nodes at {pending_ips} did not become Ready. "
            f"Latest node status: {last_node_summary}"
        )

    workers = [node["ip"] for node in inventory["nodes"] if node["role"] == "worker"]
    # `talosctl health` accepts a single configured target node. The
    # control-plane and worker lists below tell it which cluster members to
    # validate; a multi-address `--nodes` target is not supported by health.
    run(["talosctl", "config", "node", controlplanes[0]], env=env)
    run([
        "talosctl", "health",
        "--nodes", controlplanes[0],
        "--control-plane-nodes", ",".join(controlplanes),
        "--worker-nodes", ",".join(workers),
        "--init-node", controlplanes[0],
        "--endpoints", ",".join(controlplanes),
        "--wait-timeout", "10m",
    ], env=env)

    kubeconfig_text = kubeconfig.read_text(encoding="utf-8")
    writer = login("WRITE")
    existing_kubeconfig_entry = get_secret(read_token, KUBECONFIG_NAME)
    write_secret(
        writer, KUBECONFIG_NAME, kubeconfig_text,
        existed=existing_kubeconfig_entry is not None,
    )
    if read_secret(read_token, KUBECONFIG_NAME) != kubeconfig_text:
        raise RuntimeError("Infisical did not verify the saved kubeconfig")
    existing_marker_entry = get_secret(read_token, INITIALIZED_NAME)
    existing_marker = secret_value(existing_marker_entry)
    if existing_marker not in (None, inventory["cluster_name"]):
        raise RuntimeError("Infisical initialization marker belongs to a different cluster name")
    writer = login("WRITE")
    write_secret(
        writer, INITIALIZED_NAME, inventory["cluster_name"],
        existed=existing_marker_entry is not None,
    )
    if read_secret(read_token, INITIALIZED_NAME) != inventory["cluster_name"]:
        raise RuntimeError("Infisical did not verify the cluster initialization marker")
    print("Talos control planes are healthy, Cilium is ready, and all five Kubernetes nodes report Ready.")
    print("Kubeconfig is stored in Infisical. Flux and application workloads are the next delivery stage.")


def main():
    inventory = load_inventory()
    action = required("TALOS_ACTION").lower()
    if action not in ("plan", "apply"):
        raise RuntimeError("TALOS_ACTION must be plan or apply")
    initialize = os.environ.get("INITIALIZE_CLUSTER", "false").lower() == "true"
    read_token = login("READ")
    secrets_entry = get_secret(read_token, SECRETS_NAME)
    secrets = secret_value(secrets_entry)
    initialized = read_secret(read_token, INITIALIZED_NAME)
    if initialized and initialized != inventory["cluster_name"]:
        raise RuntimeError("Infisical initialization marker does not match this inventory; refusing to touch the cluster")
    talosconfig = WORKDIR / "talosconfig"
    WORKDIR.mkdir(mode=0o700, parents=True, exist_ok=True)

    if secrets:
        secrets_file = WORKDIR / "secrets.yaml"
        secrets_file.write_text(secrets, encoding="utf-8")
        os.chmod(secrets_file, 0o600)
        run(["talosctl", "gen", "config", inventory["cluster_name"], f"https://{inventory['api_vip']}:6443",
             "--with-secrets", str(secrets_file), "--output-types", "talosconfig", "--output", str(talosconfig),
             "--force"])
        os.chmod(talosconfig, 0o600)
        env = talos_environment(talosconfig)
        if action == "plan":
            try:
                existing_cluster_health(inventory, talosconfig)
            except RuntimeError:
                if initialized or not initialize:
                    raise
                assert_fresh_maintenance(inventory)
                print("Previously saved secrets are verified and all nodes remain fresh; an approved APPLY can resume the interrupted first install.")
        else:
            try:
                existing_cluster_health(inventory, talosconfig)
            except RuntimeError:
                if initialized or not initialize:
                    raise
                # A failed earlier first run may have saved secrets but not yet
                # changed any node. Resume only if every target still proves to
                # be in maintenance mode and the API VIP remains unused.
                assert_fresh_maintenance(inventory)
                install_cluster_from_secrets(inventory, secrets_file, read_token)
                return
            configure_cilium(inventory, env, read_token)
        return

    if not initialize:
        raise RuntimeError("No Talos secrets exist in Infisical. Select INITIALIZE_CLUSTER only for a genuinely fresh five-node maintenance-mode cluster.")
    if initialized:
        raise RuntimeError("Infisical records this cluster as initialized. Refusing to reinstall or bootstrap it; use the separate recovery workflow.")
    if action == "plan":
        assert_fresh_maintenance(inventory)
        print("Fresh-cluster prerequisites passed. APPLY with INITIALIZE_CLUSTER selected will save secrets, install Talos, and bootstrap once.")
        return
    initialize_cluster(
        inventory, read_token, secrets_existed=secrets_entry is not None,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Talos cluster setup failed: {exc}", file=sys.stderr)
        sys.exit(1)
