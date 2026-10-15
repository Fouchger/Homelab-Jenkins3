#!/usr/bin/env python3
"""Guarded Flux install and Git sync for the existing Talos homelab cluster."""

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[3]
INVENTORY = ROOT / "kubernetes/talos/cluster.json"
FLUX_VERSION = "2.9.6"
FLUX_SHA256 = "b4d22673e9246cbd628881f1a9ef3b090085dced291e42d804555cee8e8d42c5"
REPOSITORY = "https://github.com/Fouchger/Homelab-Jenkins3.git"
BRANCH = "main"
SECRET_PATH = "/kubernetes/talos"
KUBECONFIG_SECRET = "KUBECONFIG"
WORKDIR = Path(os.environ.get("WORKSPACE", ".")) / ".flux-private"


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required setting {name} is missing")
    return value


def request_json(url, *, method="GET", token=None, form=None):
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
        raise RuntimeError(f"Infisical request failed with HTTP {exc.code}; response suppressed") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Infisical request failed ({type(exc).__name__})") from None
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("Infisical returned invalid JSON") from None


def infisical_token():
    result = request_json(
        f"{required('INFISICAL_URL').rstrip('/')}/api/v1/auth/universal-auth/login",
        method="POST",
        form={
            "clientId": required("INFISICAL_READ_CLIENT_ID"),
            "clientSecret": required("INFISICAL_READ_CLIENT_SECRET"),
        },
    )
    token = result.get("accessToken") if isinstance(result, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError("Infisical read identity authentication failed")
    return token


def read_kubeconfig(token):
    query = urllib.parse.urlencode({
        "projectId": required("INFISICAL_PROJECT_ID"),
        "environment": required("INFISICAL_ENVIRONMENT"),
        "secretPath": SECRET_PATH,
    })
    url = f"{required('INFISICAL_URL').rstrip('/')}/api/v4/secrets/{KUBECONFIG_SECRET}?{query}"
    result = request_json(url, token=token)
    secret = result.get("secret") if isinstance(result, dict) else None
    value = secret.get("secretValue") if isinstance(secret, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Infisical secret {SECRET_PATH}/{KUBECONFIG_SECRET} is missing or empty")
    if "apiVersion:" not in value or "clusters:" not in value or "current-context:" not in value:
        raise RuntimeError("Infisical KUBECONFIG does not look like a complete Kubernetes kubeconfig")
    return value


def run(args, *, env, capture=False, timeout=180):
    print("$ " + " ".join(str(arg) for arg in args), flush=True)
    result = subprocess.run(
        [str(arg) for arg in args], env=env, text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        timeout=timeout, check=False,
    )
    if capture and result.stdout:
        print(result.stdout.rstrip(), flush=True)
    if result.returncode:
        raise RuntimeError(f"Command failed with exit code {result.returncode}: {args[0]}")
    return result.stdout or ""


def load_inventory():
    data = json.loads(INVENTORY.read_text(encoding="utf-8"))
    expected = {node["ip"] for node in data["nodes"]}
    if len(expected) != 5 or len(data["nodes"]) != 5:
        raise RuntimeError("Talos inventory must contain five unique node addresses")
    network = ipaddress.ip_network(data["node_subnet"], strict=True)
    if any(ipaddress.ip_address(address) not in network for address in expected):
        raise RuntimeError("Talos inventory contains an address outside the node subnet")
    return data, expected


def flux_binary():
    binary = WORKDIR / "bin/flux"
    binary.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if binary.exists():
        digest = hashlib.sha256(binary.read_bytes()).hexdigest()
        if digest == FLUX_SHA256:
            return binary
        binary.unlink()
    url = f"https://github.com/fluxcd/flux2/releases/download/v{FLUX_VERSION}/flux_{FLUX_VERSION}_linux_amd64.tar.gz"
    archive_path = None
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            with tempfile.NamedTemporaryFile(prefix="flux-", suffix=".tar.gz", dir=WORKDIR, delete=False) as archive:
                shutil.copyfileobj(response, archive)
                archive_path = Path(archive.name)
        digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        if digest != FLUX_SHA256:
            raise RuntimeError("Downloaded Flux CLI archive failed the pinned SHA-256 check")
        with tarfile.open(archive_path, "r:gz") as archive:
            member = next((item for item in archive.getmembers() if item.name == "flux" and item.isfile()), None)
            if member is None:
                raise RuntimeError("Pinned Flux CLI archive did not contain the flux executable")
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError("Could not read flux executable from its verified archive")
            binary.write_bytes(source.read())
        binary.chmod(0o700)
    except (urllib.error.URLError, TimeoutError, tarfile.TarError) as exc:
        raise RuntimeError(f"Could not obtain the pinned Flux CLI ({type(exc).__name__})") from None
    finally:
        if archive_path:
            archive_path.unlink(missing_ok=True)
    return binary


def kubectl_json(env, *args):
    output = run(["kubectl", *args, "-o", "json"], env=env, capture=True)
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        raise RuntimeError("kubectl returned invalid JSON while checking cluster state") from None


def verify_cluster(env, inventory, expected_ips):
    endpoint = run(["kubectl", "config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}"], env=env, capture=True).strip()
    expected_endpoint = f"https://{inventory['api_vip']}:6443"
    if endpoint.rstrip("/") != expected_endpoint:
        raise RuntimeError(f"KUBECONFIG points to {endpoint or 'no API endpoint'}, expected {expected_endpoint}")
    run(["kubectl", "get", "--raw=/readyz"], env=env, capture=True, timeout=30)
    nodes = kubectl_json(env, "get", "nodes")
    ready_ips = set()
    for node in nodes.get("items", []):
        conditions = {condition.get("type"): condition.get("status") for condition in node.get("status", {}).get("conditions", [])}
        addresses = {item.get("address") for item in node.get("status", {}).get("addresses", []) if item.get("type") == "InternalIP"}
        if conditions.get("Ready") == "True":
            ready_ips.update(addresses)
    missing = expected_ips - ready_ips
    if missing:
        raise RuntimeError("Cluster preflight requires all five inventory nodes Ready; missing: " + ", ".join(sorted(missing)))
    print(f"Kubernetes API {expected_endpoint} is healthy; all five Talos inventory nodes report Ready.", flush=True)


def inspect_flux(env):
    namespace = run(["kubectl", "get", "namespace", "flux-system", "--ignore-not-found", "-o", "name"], env=env, capture=True).strip()
    deployments = []
    if namespace:
        deployments = kubectl_json(env, "-n", "flux-system", "get", "deployments").get("items", [])
    controller_names = {item.get("metadata", {}).get("name") for item in deployments}
    expected = {"source-controller", "kustomize-controller", "helm-controller", "notification-controller"}
    if controller_names and not expected.issubset(controller_names):
        missing = ", ".join(sorted(expected - controller_names))
        raise RuntimeError(f"Found a partial Flux installation; missing controllers: {missing}. Inspect it before proceeding.")
    if controller_names:
        for deployment in deployments:
            name = deployment.get("metadata", {}).get("name")
            if name in expected:
                desired = deployment.get("spec", {}).get("replicas", 1)
                available = deployment.get("status", {}).get("availableReplicas", 0)
                if available < desired:
                    raise RuntimeError(f"Existing Flux controller {name} is not ready ({available}/{desired}); refusing to change it")
        print("Flux controllers are already installed and ready; this run will only ensure the Git source and sync exist.", flush=True)
    elif namespace:
        raise RuntimeError("flux-system exists but has no controllers; inspect the partial installation before proceeding")
    else:
        print(f"Flux is not installed. APPLY will install the committed Flux v{FLUX_VERSION} manifests.", flush=True)
    source = ""
    sync = ""
    if controller_names:
        source = run(["kubectl", "-n", "flux-system", "get", "gitrepository", "homelab", "--ignore-not-found", "-o", "json"], env=env, capture=True).strip()
        sync = run(["kubectl", "-n", "flux-system", "get", "kustomization", "homelab", "--ignore-not-found", "-o", "json"], env=env, capture=True).strip()
    if bool(source) != bool(sync):
        raise RuntimeError("Found only one of the homelab GitRepository/Kustomization objects; inspect the partial Flux sync before proceeding")
    if controller_names and not source:
        raise RuntimeError("Flux is installed without the expected homelab Git sync; migrate it deliberately before using Job 011")
    if source:
        try:
            source_spec = json.loads(source).get("spec", {})
            sync_spec = json.loads(sync).get("spec", {})
        except json.JSONDecodeError:
            raise RuntimeError("Could not safely inspect the existing homelab Flux sync") from None
        if (source_spec.get("url") != REPOSITORY or source_spec.get("ref", {}).get("branch") != BRANCH
                or sync_spec.get("path") != "./kubernetes/clusters/homelab"
                or sync_spec.get("sourceRef", {}).get("name") != "homelab"):
            raise RuntimeError("An existing homelab Flux sync points to a different repository, branch, or path; refusing to replace it")
    return bool(controller_names)


def verify_manifests():
    manifest_dir = ROOT / "kubernetes/clusters/homelab/flux-system"
    required_paths = (
        manifest_dir / "kustomization.yaml",
        manifest_dir / "gotk-components.yaml",
        manifest_dir / "gotk-sync.yaml",
        ROOT / "kubernetes/clusters/homelab/kustomization.yaml",
        ROOT / "kubernetes/clusters/homelab/infrastructure/networking/kustomization.yaml",
        ROOT / "kubernetes/clusters/homelab/infrastructure/networking/cilium-lb-ip-pool.yaml",
        ROOT / "kubernetes/clusters/homelab/infrastructure/networking/cilium-l2-announcement-policy.yaml",
        ROOT / "kubernetes/clusters/homelab/infrastructure/networking/gateway.yaml",
        ROOT / "kubernetes/clusters/homelab/infrastructure/storage/kustomization.yaml",
        ROOT / "kubernetes/clusters/homelab/infrastructure/storage/nfs-csi.yaml",
        ROOT / "kubernetes/clusters/homelab/infrastructure/storage/nfs-storage-class.yaml",
    )
    for path in required_paths:
        if not path.is_file():
            raise RuntimeError(f"Required Flux GitOps manifest is missing: {path.relative_to(ROOT)}")


def wait_flux(env, flux):
    run([flux, "check", "--timeout=5m"], env=env, capture=True, timeout=330)
    run([flux, "reconcile", "source", "git", "homelab", "--namespace=flux-system", "--timeout=5m"], env=env, capture=True, timeout=330)
    run([flux, "reconcile", "kustomization", "homelab", "--namespace=flux-system", "--with-source", "--timeout=5m"], env=env, capture=True, timeout=330)
    run([flux, "get", "sources", "git", "--namespace=flux-system"], env=env, capture=True)
    run([flux, "get", "kustomizations", "--namespace=flux-system"], env=env, capture=True)


def main():
    action = required("FLUX_ACTION")
    if action not in {"plan", "apply"}:
        raise RuntimeError("FLUX_ACTION must be plan or apply")
    inventory, expected_ips = load_inventory()
    verify_manifests()
    WORKDIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    print("Authenticating to Infisical and retrieving the existing cluster kubeconfig.", flush=True)
    kubeconfig_text = read_kubeconfig(infisical_token())
    kubeconfig = WORKDIR / "kubeconfig"
    kubeconfig.write_text(kubeconfig_text, encoding="utf-8")
    kubeconfig.chmod(0o600)
    env = os.environ.copy()
    env["KUBECONFIG"] = str(kubeconfig)
    verify_cluster(env, inventory, expected_ips)
    installed = inspect_flux(env)
    if action == "plan":
        print("Read-only preflight passed. No cluster resources were changed.", flush=True)
        return

    flux = flux_binary()
    run([flux, "version", "--client"], env=env, capture=True)
    if not installed:
        run(["kubectl", "apply", "-f", ROOT / "kubernetes/clusters/homelab/flux-system/gotk-components.yaml"], env=env, capture=True, timeout=600)
        run([flux, "check", "--timeout=5m"], env=env, capture=True, timeout=330)
        run(["kubectl", "apply", "-f", ROOT / "kubernetes/clusters/homelab/flux-system/gotk-sync.yaml"], env=env, capture=True, timeout=180)
    wait_flux(env, flux)
    print("Flux installation and first repository reconciliation completed successfully.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, KeyError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"Flux bootstrap failed: {exc}", flush=True)
        raise SystemExit(1)
