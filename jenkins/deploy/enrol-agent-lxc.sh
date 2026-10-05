#!/usr/bin/env bash
# Enrol the WebSocket agent from Proxmox without exposing its inbound secret.
set -Eeuo pipefail
umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/../config/install.conf"
controller_id=${1:-$CONTROLLER_CTID}
agent_id=${2:-$AGENT_CTID}
[[ $EUID -eq 0 ]] || { printf 'Run enrolment as root on Proxmox.\n' >&2; exit 1; }
[[ $controller_id =~ ^[1-9][0-9]+$ && $agent_id =~ ^[1-9][0-9]+$ && $controller_id != "$agent_id" ]] || exit 2
for container_id in "$controller_id" "$agent_id"; do
  if [[ $(pct status "$container_id" 2>/dev/null || true) != 'status: running' ]]; then
    printf 'Enrolment deferred: both controller and agent must be running and installed.\n'
    exit 0
  fi
done
if ! pct exec "$controller_id" -- test -s /var/lib/homelab/jenkins-controller-installed || \
   ! pct exec "$agent_id" -- test -s /var/lib/homelab/jenkins-agent-installed; then
  printf 'Enrolment deferred: the other application installer has not completed.\n'
  exit 0
fi
mkdir -p /run/lock
exec 8>"/run/lock/homelab-jenkins-enrol-${controller_id}-${agent_id}.lock"
flock -n 8 || { printf 'Another enrolment is in progress; rerun this hook afterwards.\n' >&2; exit 1; }
controller_secret=/var/lib/jenkins/secrets/homelab-agent-bootstrap.secret
pending_marker=/var/lib/jenkins/secrets/homelab-agent-enrollment.pending
guest_secret=/run/homelab-agent-enrol.secret
# Re-export through the existing startup hook when recovering/rerunning.
if ! pct exec "$controller_id" -- test -s "$controller_secret"; then
  pct exec "$controller_id" -- install -o jenkins -g jenkins -m 0600 /dev/null "$pending_marker"
  pct exec "$controller_id" -- systemctl restart jenkins
fi
secret_ready=no
for ((attempt=1; attempt<=120; attempt++)); do
  if pct exec "$controller_id" -- test -s "$controller_secret"; then secret_ready=yes; break; fi
  sleep 1
done
[[ $secret_ready == yes ]] || { printf 'Controller did not provide its agent secret.\n' >&2; exit 1; }
temp_dir=$(mktemp -d)
cleanup() {
  rm -rf -- "$temp_dir"
  pct exec "$agent_id" -- rm -f -- "$guest_secret" >/dev/null 2>&1 || true
}
trap cleanup EXIT
pct exec "$controller_id" -- cat "$controller_secret" > "$temp_dir/agent.secret"
grep -Eq '^[[:alnum:]]{32,128}$' "$temp_dir/agent.secret" || {
  printf 'Controller returned an invalid agent secret.\n' >&2; exit 1;
}
pct push "$agent_id" "$temp_dir/agent.secret" "$guest_secret" --perms 0600
pct exec "$agent_id" -- env "HOMELAB_JENKINS_URL=$HOMELAB_JENKINS_URL" \
  /usr/local/sbin/configure-jenkins-agent --secret-file "$guest_secret"
# Only remove the controller handoff file after agent service configuration succeeds.
pct exec "$controller_id" -- rm -f -- "$controller_secret"
printf 'WebSocket agent service configured; node jenkins-agent uses label homelab-automation.\n'
