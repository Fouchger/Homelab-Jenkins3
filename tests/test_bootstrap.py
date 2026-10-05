#!/usr/bin/env python3
"""Jenkins bootstrap tests: mock Proxmox/downloads without provisioning services."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.trace = self.base / 'trace'
        self.env = dict(os.environ, PATH=f'{self.base}:{os.environ["PATH"]}',
                        TEST_STATE=str(self.base / 'created'), TEST_TRACE=str(self.trace),
                        TEST_TARGET_CTID='100')
        self.command('curl', '''#!/bin/bash
while (($#)); do
  if [[ $1 == -o ]]; then target=$2; shift 2; else shift; fi
done
case ${TEST_DOWNLOAD:-ok} in
  fail) exit 22;;
  empty) : > "$target";;
  invalid) printf 'if then\\n' > "$target";;
  *) cat > "$target" <<'INSTALLER'
#!/bin/bash
[[ -z $var_post_install ]] || exit 19
printf 'installer\\n' >> "$TEST_TRACE"
touch "$TEST_STATE"
INSTALLER
esac
''')
        self.command('pct', '''#!/bin/bash
printf '%s\\n' "$*" >> "$TEST_TRACE"
case $1 in
  status)
    [[ -f $TEST_STATE ]] || exit 1
    [[ $2 == $TEST_TARGET_CTID || ${TEST_OTHER_PRESENT:-0} == 1 ]] || exit 1
    printf 'status: running\\n';;
  exec)
    if [[ $* == *install-controller-guest.sh* || $* == *install-agent-guest.sh* ]]; then
      exit "${TEST_GUEST_RESULT:-0}"
    fi
    if [[ $* == *'test -s /var/lib/homelab/'* && ${TEST_OTHER_READY:-1} == 0 ]]; then exit 1; fi
    if [[ $* == *'cat /var/lib/jenkins/secrets/homelab-agent-bootstrap.secret'* ]]; then
      printf '%s\\n' "${TEST_SECRET:-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}"
    fi
    if [[ $* == *configure-jenkins-agent* ]]; then exit "${TEST_ENROL_RESULT:-0}"; fi
    if [[ $* == *'test -e /run/homelab-'* ]]; then exit 1; fi
    if [[ $* == *credentials.xml* && ${TEST_IMPORT_SAVED:-1} == 0 ]]; then exit 1; fi;;
  push) [[ -f $3 ]] || exit 1;;
  *) exit 1;;
esac
''')

    def command(self, name, content):
        path = self.base / name
        path.write_text(content)
        path.chmod(0o755)

    def launch(self, profile='controlplane', extra=None):
        environment = dict(self.env, **(extra or {}))
        environment['TEST_TARGET_CTID'] = '101' if profile == 'jenkins-agent' else '100'
        path = ROOT / f'proxmox_helper_script/lxc/ubuntu/{profile}.profile.sh'
        return subprocess.run(['bash', str(ROOT / 'proxmox_helper_script/create-lxc.sh'), str(path)],
                              env=environment, capture_output=True, text=True)

    def test_controller_hook_runs_after_creation(self):
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        trace = self.trace.read_text()
        self.assertLess(trace.index('installer\n'), trace.index('push 100'))
        self.assertIn('install-controller-guest.sh', trace)
        self.assertIn('JENKINS_PORT=8080', trace)
        self.assertIn('INSTALL_TIMEZONE=Pacific/Auckland', trace)

    def test_agent_hook_receives_correct_container_and_tools_script(self):
        result = self.launch('jenkins-agent')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        trace = self.trace.read_text()
        self.assertIn('push 101', trace)
        self.assertIn('install-agent-guest.sh', trace)
        self.assertIn('HOMELAB_JENKINS_URL=http://192.168.20.5:8080', trace)

    def test_download_failure_propagates(self):
        result = self.launch(extra={'TEST_DOWNLOAD': 'fail'})
        self.assertEqual(result.returncode, 22)
        self.assertFalse((self.base / 'created').exists())

    def test_empty_download_fails(self):
        self.assertNotEqual(self.launch(extra={'TEST_DOWNLOAD': 'empty'}).returncode, 0)
        self.assertFalse((self.base / 'created').exists())

    def test_invalid_download_fails(self):
        self.assertNotEqual(self.launch(extra={'TEST_DOWNLOAD': 'invalid'}).returncode, 0)
        self.assertFalse((self.base / 'created').exists())

    def test_guest_failure_reaches_launcher(self):
        result = self.launch(extra={'TEST_GUEST_RESULT': '42'})
        self.assertEqual(result.returncode, 42)
        self.assertNotIn('installation verified', result.stdout)

    def test_existing_container_rejected_before_creation(self):
        (self.base / 'created').touch()
        result = self.launch()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('installer\n', self.trace.read_text())

    def test_missing_hook_rejected_before_download(self):
        profile = self.base / 'missing.profile.sh'
        profile.write_text('# Missing-hook fixture\nLXC_DEFAULT_OS=ubuntu\n'
                           'LXC_DEFAULT_CTID=999\n'
                           'LXC_DEFAULT_HOST_POST_INSTALL_SCRIPT=/does-not-exist\n'
                           'LXC_PROFILE_INSTALLER_URL=https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/ct/ubuntu.sh\n')
        result = subprocess.run(['bash', str(ROOT / 'proxmox_helper_script/create-lxc.sh'), str(profile)],
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('missing or unreadable', result.stderr)
        self.assertFalse((self.base / 'created').exists())

    def test_first_container_defers_enrolment(self):
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Enrolment deferred', result.stdout)
        self.assertNotIn('configure-jenkins-agent', self.trace.read_text())

    def test_second_container_enrols_without_printing_secret(self):
        result = self.launch('jenkins-agent', {'TEST_OTHER_PRESENT': '1'})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        trace = self.trace.read_text()
        self.assertIn('configure-jenkins-agent --secret-file', trace)
        self.assertIn('push 101', trace)
        self.assertNotIn('a' * 64, result.stdout + result.stderr + trace)
        self.assertIn('exec 100 -- rm -f -- /var/lib/jenkins/secrets/homelab-agent-bootstrap.secret', trace)

    def test_enrolment_failure_propagates_and_preserves_controller_secret(self):
        result = self.launch('jenkins-agent', {'TEST_OTHER_PRESENT': '1', 'TEST_ENROL_RESULT': '37'})
        self.assertEqual(result.returncode, 37)
        trace = self.trace.read_text()
        self.assertNotIn('exec 100 -- rm -f -- /var/lib/jenkins/secrets/homelab-agent-bootstrap.secret', trace)
        self.assertIn('exec 101 -- rm -f -- /run/homelab-agent-enrol.secret', trace)

    def test_invalid_handoff_secret_rejected(self):
        result = self.launch('jenkins-agent', {'TEST_OTHER_PRESENT': '1', 'TEST_SECRET': 'bad'})
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('configure-jenkins-agent', self.trace.read_text())

    def test_unfinished_other_installer_defers_enrolment(self):
        result = self.launch('jenkins-agent', {'TEST_OTHER_PRESENT': '1', 'TEST_OTHER_READY': '0'})
        self.assertEqual(result.returncode, 0)
        self.assertIn('other application installer has not completed', result.stdout)
        self.assertNotIn('configure-jenkins-agent', self.trace.read_text())

    def import_credentials(self, mode=0o600, extra=None):
        directory = self.base / 'credentials'
        directory.mkdir(mode=0o700)
        token = directory / 'homelab-github-readonly.token'
        token.write_text('github_pat_testfixture_only_never_a_real_token\n')
        token.chmod(mode)
        (self.base / 'created').touch()
        result = subprocess.run(['bash', str(ROOT / 'jenkins/deploy/import-controller-credentials-lxc.sh'),
                                 str(directory), '100'], env=dict(self.env, **(extra or {})),
                                text=True, capture_output=True)
        return result, token

    def test_credential_import_preserves_host_source_without_printing_value(self):
        result, token = self.import_credentials()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(token.exists())
        self.assertNotIn('github_pat_testfixture', result.stdout + result.stderr + self.trace.read_text())

    def test_world_readable_credential_rejected_before_transfer(self):
        result, _ = self.import_credentials(mode=0o644)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('push 100', self.trace.read_text())

    def test_failed_credential_save_reports_failure(self):
        result, token = self.import_credentials(extra={'TEST_IMPORT_SAVED': '0'})
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(token.exists())
        self.assertIn('Credential was not saved', result.stderr)


if __name__ == '__main__':
    unittest.main()
