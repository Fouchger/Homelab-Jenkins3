"""Offline checks for safe Proxmox API-token rotation behavior."""

import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "provision_proxmox_access",
    ROOT / "jenkins/pipelines/proxmox-access/provision-proxmox-access.py",
)
proxmox_access = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxmox_access)


class ProxmoxAccessTests(unittest.TestCase):
    def test_new_token_uses_privilege_separation_and_scoped_token_acls(self):
        captured = {}

        def fake_ssh(host, script, private_key, known_hosts, timeout=90):
            captured["script"] = script
            return '{"full-tokenid":"homelab-automation@pve!jenkins-42","value":"secret-value"}'

        with patch.object(proxmox_access, "ssh_script", side_effect=fake_ssh):
            full_id, secret = proxmox_access.create_remote_token(
                "pve.example", "homelab-automation@pve", "HomelabLxcOperator",
                "jenkins-42", ["local", "local-lvm"], "private-key", "host-key",
            )

        self.assertEqual(full_id, "homelab-automation@pve!jenkins-42")
        self.assertEqual(secret, "secret-value")
        script = captured["script"]
        self.assertIn('-privsep 1', script)
        self.assertIn('pveum acl modify /vms -token "$full_id"', script)
        self.assertIn('pveum acl modify /storage/local-lvm -token "$full_id"', script)
        self.assertIn('pveum user token remove "$user" "$token_name"', script)

    def test_rollback_attempts_token_revocation_when_infisical_restore_fails(self):
        with patch.object(proxmox_access, "restore_secrets", side_effect=RuntimeError("write failed")), \
             patch.object(proxmox_access, "revoke_old_token", return_value=True) as revoke:
            restored, revoked = proxmox_access.rollback_new_token(
                "pve.example", "homelab-automation@pve", "homelab-automation@pve!jenkins-42",
                "homelab-automation@pve!old", "old-secret", "https://infisical.example",
                "writer-token", "private-key", "host-key",
            )

        self.assertFalse(restored)
        self.assertTrue(revoked)
        revoke.assert_called_once()


if __name__ == "__main__":
    unittest.main()
