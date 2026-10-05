#!/usr/bin/env bash
# Shared Proxmox host functions for installing Jenkins applications inside LXCs.
install_in_lxc() {
  local role=$1 container_id=$2 attempt ready=no
  local guest_dir=/opt/homelab/jenkins/install
  [[ $EUID -eq 0 ]] || { printf 'Run this hook as root on Proxmox.\n' >&2; return 1; }
  [[ $container_id =~ ^[1-9][0-9]+$ ]] || { printf 'A numeric CTID is required.\n' >&2; return 2; }
  command -v pct >/dev/null || { printf 'Proxmox pct is required.\n' >&2; return 1; }
  command -v flock >/dev/null || return 1
  [[ $(pct status "$container_id") == 'status: running' ]] || {
    printf 'Container %s must be running.\n' "$container_id" >&2; return 1;
  }
  source "$script_dir/../config/install.conf"
  mkdir -p /var/log/homelab /run/lock
  # Prevent overlapping bootstrap runs for the same container.
  exec 9>"/run/lock/homelab-jenkins-${container_id}.lock"
  flock -n 9 || { printf 'Installation already running for CTID %s.\n' "$container_id" >&2; return 1; }
  exec > >(tee -a "/var/log/homelab/jenkins-${role}-${container_id}.log") 2>&1
  printf '[%s] Installing Jenkins %s in CTID %s\n' "$(date -Is)" "$role" "$container_id"
  for ((attempt=1; attempt<=30; attempt++)); do
    if pct exec "$container_id" -- test -f /etc/os-release; then ready=yes; break; fi
    sleep 2
  done
  [[ $ready == yes ]] || { printf 'Container did not become ready.\n' >&2; return 1; }
  pct exec "$container_id" -- install -d -m 0755 "$guest_dir"
  local filename
  for filename in guest-common.sh "install-${role}-guest.sh"; do
    [[ -r $script_dir/$filename ]] || { printf 'Missing installer: %s\n' "$filename" >&2; return 1; }
    pct push "$container_id" "$script_dir/$filename" "$guest_dir/$filename" --perms 0755
  done
  # Public settings only; credentials are imported from protected files separately.
  pct exec "$container_id" -- env \
    "JENKINS_PORT=${JENKINS_PORT:-8080}" \
    "JENKINS_VERSION=${JENKINS_VERSION:-}" \
    "INSTALL_TIMEZONE=${INSTALL_TIMEZONE:-Pacific/Auckland}" \
    "HOMELAB_JENKINS_URL=$HOMELAB_JENKINS_URL" \
    "HOMELAB_GITHUB_OWNER=$HOMELAB_GITHUB_OWNER" \
    "HOMELAB_GITHUB_REPOSITORY=$HOMELAB_GITHUB_REPOSITORY" \
    "HOMELAB_GITHUB_BRANCH=$HOMELAB_GITHUB_BRANCH" \
    "HOMELAB_GITHUB_CREDENTIAL_ID=$HOMELAB_GITHUB_CREDENTIAL_ID" \
    "HOMELAB_INFISICAL_CREDENTIAL_ID=$HOMELAB_INFISICAL_CREDENTIAL_ID" \
    "HOMELAB_INFISICAL_URL=$HOMELAB_INFISICAL_URL" \
    "HOMELAB_INFISICAL_PROJECT_ID=$HOMELAB_INFISICAL_PROJECT_ID" \
    "HOMELAB_INFISICAL_ENVIRONMENT=$HOMELAB_INFISICAL_ENVIRONMENT" \
    "HOMELAB_INFISICAL_PROJECT_SLUG=$HOMELAB_INFISICAL_PROJECT_SLUG" \
    bash "$guest_dir/install-${role}-guest.sh"
  if [[ $role == controller ]]; then CONTROLLER_CTID=$container_id; else AGENT_CTID=$container_id; fi
  bash "$script_dir/enrol-agent-lxc.sh" "$CONTROLLER_CTID" "$AGENT_CTID"
  printf '[%s] Jenkins %s installation verified in CTID %s.\n' "$(date -Is)" "$role" "$container_id"
}
