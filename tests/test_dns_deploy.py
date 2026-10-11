"""Focused offline checks for DNS deployment configuration and API calls."""

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path, *, paramiko_stub=False):
    if paramiko_stub:
        paramiko = types.ModuleType("paramiko")
        paramiko.hostkeys = types.ModuleType("paramiko.hostkeys")
        paramiko.hostkeys.HostKeyEntry = object
        sys.modules.setdefault("paramiko", paramiko)
        sys.modules.setdefault("paramiko.hostkeys", paramiko.hostkeys)
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deploy = load_module(
    "dns_deploy", "jenkins/pipelines/dns-deploy/deploy-dns.py", paramiko_stub=True
)
network_settings = load_module(
    "network_settings", "jenkins/pipelines/network-settings/save-network-settings.py"
)
manage = load_module("manage_technitium", "jenkins/pipelines/dns-deploy/manage-technitium.py")
router = load_module(
    "apply_mikrotik_config", "jenkins/pipelines/mikrotik-config/apply-mikrotik-config.py"
)
audit = load_module(
    "audit_infisical", "jenkins/pipelines/infisical-audit/audit-infisical.py"
)


class DnsDeployTests(unittest.TestCase):
    CONFIG = """hostname: dns01
net0: name=eth0,bridge=vmbr0,hwaddr=02:00:00:00:01:50,ip=dhcp,ip6=auto,tag=30,type=veth
tags: community-script;dns;homelab;managed-by-jenkins
"""

    def test_existing_container_identity_matches_exact_network_and_tags(self):
        self.assertTrue(deploy.matches_container_identity(
            self.CONFIG, "dns01", "02:00:00:00:01:50"
        ))
        for changed in (
            self.CONFIG.replace("hostname: dns01", "hostname: other"),
            self.CONFIG.replace("02:00:00:00:01:50", "02:00:00:00:01:51"),
            self.CONFIG.replace("bridge=vmbr0", "bridge=vmbr1"),
            self.CONFIG.replace("ip=dhcp", "ip=192.168.30.2/24"),
            self.CONFIG.replace("tag=30", "tag=20"),
            self.CONFIG.replace(";dns;", ";dns-old;"),
        ):
            with self.subTest(config=changed):
                self.assertFalse(deploy.matches_container_identity(
                    changed, "dns01", "02:00:00:00:01:50"
                ))

    def test_destructive_dns_backup_is_verified_and_retained_encrypted(self):
        payload = b"mock Proxmox backup data"
        archive = "/tmp/stage/backups/vzdump-lxc-150-2026_10_11-00_00_00.tar.zst"

        class FakeSftp:
            def get(self, remote_path, local_path):
                Path(local_path).write_bytes(payload)

            def close(self):
                pass

        class FakeClient:
            def open_sftp(self):
                return FakeSftp()

        def fake_remote(_client, command, timeout=900):
            if command.startswith("vzdump "):
                return f"INFO: creating vzdump archive '{archive}'"
            if command.startswith("sha256sum "):
                import hashlib
                return f"{hashlib.sha256(payload).hexdigest()}  {archive}"
            return ""

        def fake_gpg(command, **_kwargs):
            if "--symmetric" in command:
                output = Path(command[command.index("--output") + 1])
                output.write_bytes(b"encrypted mock")
            return types.SimpleNamespace(returncode=0)

        with tempfile.TemporaryDirectory() as temp:
            with patch.object(deploy, "ROOT", Path(temp)), \
                 patch.object(deploy, "remote", side_effect=fake_remote), \
                 patch.object(deploy.subprocess, "run", side_effect=fake_gpg), \
                 patch.dict(os.environ, {"BUILD_NUMBER": "17"}):
                result = deploy.backup_container(FakeClient(), "/tmp/stage", "dns01", 150,
                                                 "a" * 40)

            self.assertTrue(result.is_file())
            self.assertEqual(result.suffix, ".gpg")
            self.assertEqual(result.read_bytes(), b"encrypted mock")
            if os.name == "posix":
                self.assertEqual(result.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(result.parent.iterdir()), [result])

    def test_dns_secret_path_mapping_uses_new_layout(self):
        self.assertEqual(network_settings.CONFIG["/dns/dns01"], {
            "DNS_IPV4": "CFG_DNS01_IPV4",
            "DNS_SERVER_ADMIN_PASSWORD": "CFG_DNS01_ADMIN_PASSWORD"
        })
        self.assertEqual(network_settings.CONFIG["/dns/dns02"], {
            "DNS_IPV4": "CFG_DNS02_IPV4",
            "DNS_SERVER_ADMIN_PASSWORD": "CFG_DNS02_ADMIN_PASSWORD"
        })
        self.assertNotIn("/proxmox/dns", network_settings.CONFIG)

    def test_infisical_reader_loads_per_server_passwords(self):
        env = {
            "INFISICAL_URL": "https://infisical.example",
            "INFISICAL_READ_CLIENT_ID": "client-id",
            "INFISICAL_READ_CLIENT_SECRET": "client-secret",
            "INFISICAL_PROJECT_ID": "project-id",
            "INFISICAL_ENVIRONMENT": "prod",
        }
        secrets = {
            ("/proxmox/automation", "PVE_SSH_PRIVATE_KEY"): "private-key",
            ("/proxmox/automation", "PVE_SSH_HOST_KEY"): "host-key",
            ("/dns/dns01", "DNS_IPV4"): "192.168.30.2",
            ("/dns/dns02", "DNS_IPV4"): "192.168.30.3",
            ("/dns", "MIKROTIK_DHCP_DNS_MODE"): "router",
            ("/dns", "DNS_PUBLIC_FALLBACKS"): "1.1.1.1,1.0.0.1",
            ("/dns", "DNS_BACKUP_ENCRYPTION_PASSWORD"): "dns-backup-encryption-passphrase-2026",
            ("/dns", "DNS_HOSTED_ZONES"): "example.internal",
            ("/dns/dns01", "DNS_SERVER_ADMIN_PASSWORD"): "admin-pass-1",
            ("/dns/dns02", "DNS_SERVER_ADMIN_PASSWORD"): "admin-pass-2",
            ("/proxmox/lxc/dns01", "LXC_ROOT_PASSWORD"): "root-password-01-strong",
            ("/proxmox/lxc/dns02", "LXC_ROOT_PASSWORD"): "root-password-02-strong",
        }
        requested_paths = []

        def fake_http_json(url, method="GET", headers=None, form=None):
            if url.endswith("/api/v1/auth/universal-auth/login"):
                return {"accessToken": "test-access-token"}
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            path = query["secretPath"][0]
            name = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])
            requested_paths.append((path, name))
            return {"secret": {"secretValue": secrets[(path, name)]}}

        with patch.dict(os.environ, env, clear=True), patch.object(
            deploy, "http_json", side_effect=fake_http_json
        ):
            values = deploy.read_infisical()

        self.assertEqual(values["DNS01_ADMIN_PASSWORD"], "admin-pass-1")
        self.assertEqual(values["DNS02_ADMIN_PASSWORD"], "admin-pass-2")
        self.assertEqual(values["DNS01_ROOT_PASSWORD"], "root-password-01-strong")
        self.assertEqual(values["DNS02_ROOT_PASSWORD"], "root-password-02-strong")
        self.assertNotIn(("/proxmox/dns", "DNS01_ADMIN_PASSWORD"), requested_paths)
        self.assertIn(("/dns/dns01", "DNS_SERVER_ADMIN_PASSWORD"), requested_paths)
        self.assertIn(("/dns/dns02", "DNS_SERVER_ADMIN_PASSWORD"), requested_paths)

    def test_new_secondary_uses_documented_transfer_protocol_argument(self):
        actions = []

        def fake_call(path, form=None, token=None, allow_error=False):
            actions.append((path, form))
            if path == "/api/zones/list":
                if len([item for item in actions if item[0] == path]) == 1:
                    return {"response": {"zones": []}, "status": "ok"}
                return {"response": {"zones": [{
                    "name": "example.internal", "type": "Secondary",
                    "syncFailed": False, "soaSerial": 1,
                }]}, "status": "ok"}
            return {"status": "ok"}

        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as stream:
            json.dump({
                "admin_password": "secure-admin-pass",
                "action": "configure-secondaries",
                "primary_ipv4": "192.168.30.2",
                "zones": ["example.internal"],
            }, stream)
            config_path = stream.name
        self.addCleanup(lambda: Path(config_path).unlink(missing_ok=True))

        with patch.object(manage, "authenticate", return_value="session-token"), \
             patch.object(manage, "call", side_effect=fake_call), \
             patch.object(manage.time, "sleep"):
            with patch.object(sys, "argv", ["manage-technitium.py", config_path]):
                manage.main()

        create = next(form for path, form in actions if path == "/api/zones/create")
        self.assertEqual(create["zoneTransferProtocol"], "Tcp")
        self.assertNotIn("primaryZoneTransferProtocol", create)

    def test_router_dhcp_dns_verification_checks_live_records(self):
        class FakeRouter:
            def __init__(self, dns_rows):
                self.dns_rows = dns_rows

            def call(self, path, method="GET", payload=None):
                if path == "ip/dns":
                    return {"servers": "192.168.30.2,192.168.30.3", "allow-remote-requests": "false"}
                if path == "ip/firewall/filter":
                    return []
                if path == "ip/dhcp-server/network":
                    return self.dns_rows
                raise AssertionError(f"Unexpected RouterOS path: {path}")

        network = [("*1", "192.168.20.0/24", "192.168.20.1", "192.168.30.2,192.168.30.3")]
        good = FakeRouter([{".id": "*1", "dns-server": "192.168.30.2,192.168.30.3"}])
        router.rest_apply(good, network, "direct", "192.168.30.2,192.168.30.3", [])
        bad = FakeRouter([{".id": "*1", "dns-server": "192.168.30.2"}])
        with self.assertRaisesRegex(RuntimeError, "DHCP DNS verification failed"):
            router.rest_apply(bad, network, "direct", "192.168.30.2,192.168.30.3", [])

    def test_normal_router_jobs_use_management_host_not_reset_address(self):
        values = {"MIKROTIK_HOST": "router-mgmt.example", "MIKROTIK_IP": "192.168.99.1"}
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(router.connection_host(values), "router-mgmt.example")
        with patch.dict(os.environ, {"HOMELAB_MIKROTIK_CONNECT_HOST": "192.168.99.1"}, clear=True):
            self.assertEqual(router.connection_host(values), "192.168.99.1")

    def test_infisical_audit_only_requests_variable_metadata(self):
        env = {
            "INFISICAL_URL": "https://infisical.example",
            "INFISICAL_READ_CLIENT_ID": "client-id",
            "INFISICAL_READ_CLIENT_SECRET": "client-secret",
            "INFISICAL_PROJECT_ID": "project-id",
            "INFISICAL_ENVIRONMENT": "prod",
        }
        requests = []

        def fake_request(url, *, method="GET", headers=None, form=None):
            requests.append((url, headers, form))
            if url.endswith("/api/v1/auth/universal-auth/login"):
                return {"accessToken": "token"}
            return {"secrets": [{"secretPath": "/dns/dns01", "secretKey": "DNS_IPV4"}]}

        with patch.dict(os.environ, env, clear=True), patch.object(
            audit, "request_json", side_effect=fake_request
        ):
            actual = audit.fetch_inventory()

        query = urllib.parse.parse_qs(urllib.parse.urlsplit(requests[1][0]).query)
        self.assertEqual(query["viewSecretValue"], ["false"])
        self.assertEqual(query["recursive"], ["true"])
        self.assertEqual(actual, {("/dns/dns01", "DNS_IPV4")})
        self.assertNotIn("secretValue", repr(actual))

    def test_infisical_audit_flags_missing_required_but_not_optional(self):
        rows, failed = audit.build_report(
            set(),
            [
                {"path": "/dns/dns01", "name": "DNS_IPV4", "state": "required", "used_by": "006"},
                {"path": "/dns", "name": "DNS_HOSTED_ZONES", "state": "optional", "used_by": "006"},
                {"path": "/mikrotik", "name": "MIKROTIK_SCRIPT", "state": "conditional", "used_by": "005"},
                {"path": "/cloudflare", "name": "API_TOKEN", "state": "planned", "used_by": "future"},
            ],
        )
        self.assertTrue(failed)
        self.assertEqual([row["status"] for row in rows], ["MISSING"])

    def test_infisical_audit_reports_presence_without_claiming_value_is_set(self):
        rows, failed = audit.build_report(
            {("/dns/dns01", "DNS_IPV4")},
            [{"path": "/dns/dns01", "name": "DNS_IPV4", "state": "required", "used_by": "006"}],
        )
        self.assertFalse(failed)
        self.assertEqual(rows[0]["status"], "PRESENT")


if __name__ == "__main__":
    unittest.main()
