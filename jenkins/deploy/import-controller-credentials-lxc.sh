#!/usr/bin/env bash
# Import optional protected credentials through the controller's startup hooks.
# Usage: bash import-controller-credentials-lxc.sh /root/jenkins-bootstrap-secrets [CTID]
set -Eeuo pipefail
umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "${HOMELAB_INSTALL_CONFIG:-$script_dir/../config/install.conf}"
credential_dir=${1:-}
controller_id=${2:-$CONTROLLER_CTID}
[[ $EUID -eq 0 && $controller_id =~ ^[1-9][0-9]+$ && -d $credential_dir ]] || {
  printf 'Usage (as root): %s <protected-secret-directory> [controller-CTID]\n' "${0##*/}" >&2
  exit 2
}
[[ $(stat -c '%u' "$credential_dir") == 0 && $(stat -c '%a' "$credential_dir") == 700 ]] || {
  printf 'The secret directory must be root-owned with mode 0700.\n' >&2; exit 2;
}
files=(homelab-github-readonly.token homelab-pve01-automation-key \
  homelab-infisical-client-id homelab-infisical-client-secret \
  homelab-infisical-writer-client-id homelab-infisical-writer-client-secret \
  homelab-infisical-project-id homelab-infisical-url \
  homelab-infisical-environment homelab-infisical-project-slug \
  homelab-proxmox-host)
[[ $(pct status "$controller_id") == 'status: running' ]] || exit 1
# Validate all inputs before transferring any credentials.
found=0
for filename in "${files[@]}"; do
  path="$credential_dir/$filename"
  [[ -e $path ]] || continue
  [[ ! -L $path && -f $path && -s $path && $(stat -c '%u' "$path") == 0 && $(stat -c '%a' "$path") == 600 ]] || {
    printf 'Credential %s must be a non-empty, root-owned regular file with mode 0600.\n' "$filename" >&2; exit 2;
  }
  found=$((found+1))
done
(( found > 0 )) || { printf 'No supported credential files were found.\n' >&2; exit 2; }
for identity in read writer; do
  if [[ $identity == read ]]; then prefix=homelab-infisical; else prefix=homelab-infisical-writer; fi
  if [[ -f $credential_dir/$prefix-client-id || -f $credential_dir/$prefix-client-secret ]]; then
    [[ -f $credential_dir/$prefix-client-id && -f $credential_dir/$prefix-client-secret ]] || {
      printf 'Supply both Infisical client ID and secret for %s.\n' "$identity" >&2; exit 2;
    }
  fi
done
transferred=()
cleanup() {
  for filename in "${transferred[@]}"; do
    pct exec "$controller_id" -- rm -f -- "/run/$filename" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT
for filename in "${files[@]}"; do
  [[ -f $credential_dir/$filename ]] || continue
  transferred+=("$filename")
  pct push "$controller_id" "$credential_dir/$filename" "/run/$filename" --perms 0600
  pct exec "$controller_id" -- chown jenkins:jenkins "/run/$filename"
done
# Jenkins init hooks run as the jenkins user. They can read these protected
# files but cannot unlink entries from root-owned /run. A completed systemd
# restart means all init hooks have finished, so remove the files as root on
# the Proxmox host instead of waiting for the hooks to delete them.
printf 'Restarting Jenkins to import the supplied credentials.\n'
pct exec "$controller_id" -- systemctl restart jenkins
printf 'Jenkins restart completed; removing temporary credential files from the container.\n'
for filename in "${transferred[@]}"; do
  pct exec "$controller_id" -- rm -f -- "/run/$filename"
done
# The files are now removed; verify that the Jenkins hooks stored their IDs.
for filename in "${transferred[@]}"; do
  case $filename in
    homelab-github-readonly.token) credential_id=$HOMELAB_GITHUB_CREDENTIAL_ID;;
    homelab-pve01-automation-key) credential_id=pve01-automation-ssh;;
    homelab-infisical-client-id) credential_id=$HOMELAB_INFISICAL_CREDENTIAL_ID;;
    homelab-infisical-writer-client-id) credential_id=infisical-homelab-prod-writer;;
    homelab-infisical-project-id) credential_id=homelab-infisical-project-id;;
    homelab-infisical-url) credential_id=homelab-infisical-url;;
    homelab-infisical-environment) credential_id=homelab-infisical-environment;;
    homelab-infisical-project-slug) credential_id=homelab-infisical-project-slug;;
    homelab-proxmox-host) credential_id=homelab-proxmox-host;;
    *) continue;;
  esac
  pct exec "$controller_id" -- grep -Fq -- "$credential_id" /var/lib/jenkins/credentials.xml || {
    printf 'Credential was not saved: %s. Inspect Jenkins startup logs.\n' "$credential_id" >&2; exit 1;
  }
done
for identity in read writer; do
  if [[ $identity == read ]]; then prefix=homelab-infisical; else prefix=homelab-infisical-writer; fi
  [[ -f $credential_dir/$prefix-client-id ]] || continue
  pct exec "$controller_id" -- grep -qx valid "/var/lib/jenkins/secrets/homelab-infisical-${identity}.status" || {
    printf 'Infisical %s credential was saved but did not authenticate.\n' "$identity" >&2; exit 1;
  }
done
printf 'Credential import checks passed. Original protected files remain on the host.\n'
printf 'Confirm credentials in Jenkins, then remove those bootstrap source files.\n'
