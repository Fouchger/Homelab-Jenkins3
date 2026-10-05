#!/usr/bin/env python3
"""Standalone download-bootstrap tests with mocked Proxmox and repository downloads."""
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DownloadBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        fixture = self.base / 'fixture'
        (fixture / 'proxmox_helper_script/lxc/ubuntu').mkdir(parents=True)
        (fixture / 'jenkins/config').mkdir(parents=True)
        (fixture / 'jenkins/deploy').mkdir()
        (fixture / 'jenkins/config/install.conf').write_text(
            '# Fixture settings\nCONTROLLER_CTID=100\nAGENT_CTID=101\n'
            'HOMELAB_JENKINS_URL=http://192.168.20.5:8080\n')
        for name, ctid, mac in [('controlplane', 100, 'BC:24:11:4B:E3:DB'),
                                ('jenkins-agent', 101, 'BC:24:11:5E:D9:F9')]:
            (fixture / f'proxmox_helper_script/lxc/ubuntu/{name}.profile.sh').write_text(
                f'# Fixture profile\nLXC_DEFAULT_CTID={ctid}\n'
                f'LXC_DEFAULT_HOSTNAME={name}\nLXC_DEFAULT_MAC_ADDRESS={mac}\n')
        (fixture / 'proxmox_helper_script/create-lxc.sh').write_text('''#!/bin/bash
source "$1"
printf 'create %s\\n' "$LXC_DEFAULT_CTID" >> "$TEST_TRACE"
if [[ $LXC_DEFAULT_CTID == 101 ]]; then exit "${TEST_AGENT_RESULT:-0}"; fi
touch "$TEST_BASE/state-$LXC_DEFAULT_CTID"
''')
        for role in ['controller', 'agent']:
            (fixture / f'jenkins/deploy/install-{role}-lxc.sh').write_text(
                '#!/bin/bash\nprintf "resume %s\\n" "$1" >> "$TEST_TRACE"\n')
        (fixture / 'jenkins/deploy/enrol-agent-lxc.sh').write_text('#!/bin/bash\nexit 0\n')
        self.archive = self.base / 'repository.tar.gz'
        with tarfile.open(self.archive, 'w:gz') as archive:
            archive.add(fixture, arcname='Fouchger-Homelab-Jenkins3-fixture')
        self.trace = self.base / 'trace'
        self.env = dict(os.environ, PATH=f'{self.base}:{os.environ["PATH"]}',
                        TEST_BASE=str(self.base), TEST_ARCHIVE=str(self.archive),
                        TEST_TRACE=str(self.trace))
        self.command('curl', '''#!/bin/bash
[[ ${TEST_DOWNLOAD_RESULT:-0} == 0 ]] || exit "$TEST_DOWNLOAD_RESULT"
while (($#)); do
  if [[ $1 == -o ]]; then destination=$2; shift 2; else shift; fi
done
cp "$TEST_ARCHIVE" "$destination"
''')
        self.command('mktemp', '''#!/bin/bash
exec /usr/bin/mktemp -d "$TEST_BASE/download.XXXXXX"
''')
        self.command('pct', '''#!/bin/bash
printf '%s\\n' "$*" >> "$TEST_TRACE"
case $1 in
  status) [[ -f $TEST_BASE/state-$2 ]] || exit 1; printf 'status: running\\n';;
  config)
    if [[ ${TEST_WRONG_IDENTITY:-0} == 1 ]]; then printf 'hostname: unrelated\\nnet0: hwaddr=wrong\\n'; exit; fi
    if [[ $2 == 100 ]]; then name=controlplane; mac=BC:24:11:4B:E3:DB
    else name=jenkins-agent; mac=BC:24:11:5E:D9:F9; fi
    printf 'hostname: %s\\nnet0: name=eth0,hwaddr=%s,bridge=vmbr0,tag=20\\n' "$name" "$mac";;
  push) [[ -s $3 ]] || exit 1;;
  exec|start) exit 0;;
  *) exit 1;;
esac
''')

    def command(self, name, text):
        path = self.base / name
        path.write_text(text)
        path.chmod(0o755)

    def run_bootstrap(self, extra=None):
        # Exactly the streamed Bash execution context: no neighbouring repo files.
        content = (ROOT / 'proxmox_helper_script/controlplane.sh').read_text()
        result = subprocess.run(['bash', '-c', content], env=dict(self.env, **(extra or {})),
                                capture_output=True, text=True)
        self.assertEqual(list(self.base.glob('download.*')), [], 'Temporary project must be removed')
        return result

    def test_standalone_bootstrap_creates_both_in_order_and_cleans_up(self):
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        trace = self.trace.read_text()
        self.assertLess(trace.index('create 100'), trace.index('create 101'))
        self.assertIn('/opt/homelab/bootstrap-project', trace)
        self.assertIn('Temporary project folder removed', result.stdout)

    def test_download_failure_cleans_up_without_touching_containers(self):
        result = self.run_bootstrap({'TEST_DOWNLOAD_RESULT': '22'})
        self.assertEqual(result.returncode, 22)
        self.assertFalse(self.trace.exists())

    def test_agent_failure_preserves_controller_and_cleans_up(self):
        result = self.run_bootstrap({'TEST_AGENT_RESULT': '42'})
        self.assertEqual(result.returncode, 42)
        self.assertTrue((self.base / 'state-100').exists())
        self.assertNotIn('Both containers are installed', result.stdout)

    def test_existing_matching_containers_resume_installation(self):
        (self.base / 'state-100').touch()
        (self.base / 'state-101').touch()
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        trace = self.trace.read_text()
        self.assertIn('resume 100', trace)
        self.assertIn('resume 101', trace)
        self.assertNotIn('create 100', trace)

    def test_unrelated_existing_container_is_rejected(self):
        (self.base / 'state-100').touch()
        result = self.run_bootstrap({'TEST_WRONG_IDENTITY': '1'})
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('resume 100', self.trace.read_text())
        self.assertNotIn('create 101', self.trace.read_text())

    def test_invalid_archive_is_rejected_and_cleaned_up(self):
        self.archive.write_text('not a tar archive')
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.trace.exists())


if __name__ == '__main__':
    unittest.main()
