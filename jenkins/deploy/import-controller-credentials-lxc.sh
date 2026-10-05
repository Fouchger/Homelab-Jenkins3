#!/usr/bin/env bash
# Import optional protected credentials through the controller's startup hooks.
# Usage: bash import-controller-credentials-lxc.sh /root/jenkins-bootstrap-secrets [CTID]
set -Eeuo pipefail
umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/../config/install.conf"
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
  homelab-infisical-writer-client-id homelab-infisical-writer-client-secret)
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
pct exec "$controller_id" -- systemctl restart jenkins
complete=no
for ((attempt=1; attempt<=120; attempt++)); do
  remaining=0
  for filename in "${transferred[@]}"; do
    if pct exec "$controller_id" -- test -e "/run/$filename"; then remaining=$((remaining+1)); fi
  done
  if (( remaining == 0 )); then complete=yes; break; fi
  sleep 1
done
[[ $complete == yes ]] || { printf 'Credential hooks did not finish; inspect Jenkins logs.\n' >&2; exit 1; }
# Hooks remove temporary files even on failure: separately verify stored IDs.
for filename in "${transferred[@]}"; do
  case $filename in
    homelab-github-readonly.token) credential_id=$HOMELAB_GITHUB_CREDENTIAL_ID;;
    homelab-pve01-automation-key) credential_id=pve01-automation-ssh;;
    homelab-infisical-client-id) credential_id=$HOMELAB_INFISICAL_CREDENTIAL_ID;;
    homelab-infisical-writer-client-id) credential_id=infisical-homelab-prod-writer;;
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
