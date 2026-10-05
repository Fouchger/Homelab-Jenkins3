#!/usr/bin/env bash
# Install and verify Jenkins LTS inside the controlplane Ubuntu container.
set -Eeuo pipefail
INSTALL_ROLE=controller
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/guest-common.sh"
JENKINS_PORT=${JENKINS_PORT:-8080}
[[ $JENKINS_PORT =~ ^[0-9]+$ && ${#JENKINS_PORT} -le 5 ]] || exit 2
(( 10#$JENKINS_PORT >= 1024 && 10#$JENKINS_PORT <= 65535 )) || exit 2
JENKINS_PORT=$((10#$JENKINS_PORT))
# Install Java before Jenkins, as required by the official package instructions.
apt_install fontconfig openjdk-21-jre-headless
install -d -m 0755 /etc/apt/keyrings
key_file=$(mktemp)
trap 'rm -f -- "$key_file"' EXIT
curl --fail --silent --show-error --location --retry 3 --connect-timeout 15 --max-time 120   https://pkg.jenkins.io/debian-stable/jenkins.io-2026.key -o "$key_file"
# Validate the primary 2026 Jenkins repository signing key before trusting it.
fingerprint=$(gpg --batch --show-keys --with-colons "$key_file" | awk -F: '$1 == "fpr" {print $10; exit}')
[[ $fingerprint == 5E386EADB55F01504CAE8BCF7198F4B714ABFC68 ]] || {
  printf 'Unexpected Jenkins repository signing key; installation stopped.\n' >&2; exit 1;
}
install -m 0644 "$key_file" /etc/apt/keyrings/jenkins-keyring.asc
printf '%s\n' 'deb [signed-by=/etc/apt/keyrings/jenkins-keyring.asc] https://pkg.jenkins.io/debian-stable binary/'   > /etc/apt/sources.list.d/jenkins.list
apt-get -o DPkg::Lock::Timeout=180 update
if [[ -n ${JENKINS_VERSION:-} ]]; then
  apt_install "jenkins=$JENKINS_VERSION"
elif ! dpkg-query -W -f='${Status}' jenkins 2>/dev/null | grep -qx 'install ok installed'; then
  apt_install jenkins
fi
# Keep the setup wizard and existing Jenkins data/configuration intact.
install -d -m 0755 /etc/systemd/system/jenkins.service.d
cat > /etc/systemd/system/jenkins.service.d/20-homelab.conf <<EOF
# Homelab Jenkins service settings.
[Service]
Environment="JENKINS_PORT=$JENKINS_PORT"
EOF
systemctl daemon-reload
systemctl enable jenkins
systemctl restart jenkins
healthy=no
for ((attempt=1; attempt<=60; attempt++)); do
  if systemctl is-active --quiet jenkins &&     curl --silent --show-error --max-time 5 --dump-header /tmp/homelab-jenkins-headers       --output /dev/null "http://127.0.0.1:$JENKINS_PORT/login" &&     grep -qi '^X-Jenkins:' /tmp/homelab-jenkins-headers; then healthy=yes; break; fi
  sleep 5
done
rm -f /tmp/homelab-jenkins-headers
[[ $healthy == yes ]] || {
  printf 'Jenkins did not become healthy.\n' >&2
  journalctl -u jenkins --no-pager -n 50
  exit 1
}
java -version
dpkg-query -W jenkins
date -Is > /var/lib/homelab/jenkins-controller-installed
printf 'Jenkins is ready on port %s. Complete the initial setup in your browser.\n' "$JENKINS_PORT"
printf 'Unlock password stays in /var/lib/jenkins/secrets/initialAdminPassword; it is not printed.\n'
