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
                        TEST_STATE=str(self.base / 'created'), TEST_TRACE=str(self.trace))
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
  status) [[ -f $TEST_STATE ]] || exit 1; printf 'status: running\\n';;
  exec)
    if [[ $* == *install-controller-guest.sh* || $* == *install-agent-guest.sh* ]]; then
      exit "${TEST_GUEST_RESULT:-0}"
    fi;;
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
        self.assertIn('AGENT_USER=jenkins-agent', trace)

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


if __name__ == '__main__':
    unittest.main()
