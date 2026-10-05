#!/usr/bin/env bash
# Shared guest bootstrap: prerequisites, installation logging and APT behaviour.
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { printf 'Guest installation requires root.\n' >&2; exit 1; }
source /etc/os-release
[[ ${ID:-} == ubuntu && ${VERSION_ID:-} =~ ^(24\.04|26\.04)$ ]] || {
  printf 'Supported guest OS: Ubuntu 24.04 or 26.04.\n' >&2; exit 1;
}
[[ -d /run/systemd/system ]] || { printf 'A running systemd guest is required.\n' >&2; exit 1; }
mkdir -p /var/log/homelab /var/lib/homelab
rm -f -- "/var/lib/homelab/jenkins-${INSTALL_ROLE}-installed"
exec > >(tee -a "/var/log/homelab/jenkins-${INSTALL_ROLE}.log") 2>&1
trap 'result=$?; printf "Installation failed (exit %s, line %s). See the installation log.\n" "$result" "$LINENO" >&2; exit "$result"' ERR
export DEBIAN_FRONTEND=noninteractive
apt_install() {
  apt-get -o DPkg::Lock::Timeout=180 install -y --no-install-recommends "$@"
}
apt-get -o DPkg::Lock::Timeout=180 update
apt_install ca-certificates curl gnupg tzdata
INSTALL_TIMEZONE=${INSTALL_TIMEZONE:-Pacific/Auckland}
[[ $INSTALL_TIMEZONE != *..* && -f /usr/share/zoneinfo/$INSTALL_TIMEZONE ]] || {
  printf 'Invalid timezone: %s\n' "$INSTALL_TIMEZONE" >&2; exit 2;
}
timedatectl set-timezone "$INSTALL_TIMEZONE"
