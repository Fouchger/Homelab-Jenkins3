#!/usr/bin/env bash
# One-command Proxmox bootstrap: download project, install both LXCs and clean up.
set -Eeuo pipefail
umask 077

[[ $EUID -eq 0 ]] || { printf 'Run this bootstrap as root on Proxmox.\n' >&2; exit 1; }
for tool in curl tar mktemp pct flock tee; do
  command -v "$tool" >/dev/null || { printf 'Required host command is missing: %s\n' "$tool" >&2; exit 1; }
done
repo_owner=${HOMELAB_BOOTSTRAP_OWNER:-Fouchger}
repo_name=${HOMELAB_BOOTSTRAP_REPOSITORY:-Homelab-Jenkins3}
repo_ref=${HOMELAB_BOOTSTRAP_REF:-main}
[[ $repo_owner =~ ^[A-Za-z0-9_-]+$ && $repo_name =~ ^[A-Za-z0-9_.-]+$ && $repo_ref =~ ^[A-Za-z0-9_./-]+$ && $repo_ref != *..* ]] || exit 2
mkdir -p /var/log/homelab /run/lock
exec 7>/run/lock/homelab-jenkins-bootstrap.lock
flock -n 7 || { printf 'A Jenkins bootstrap is already running on this host.\n' >&2; exit 1; }
exec > >(tee -a /var/log/homelab/jenkins-bootstrap.log) 2>&1
temp_dir=$(mktemp -d /tmp/homelab-jenkins-bootstrap.XXXXXX)
cleanup() {
  result=$?
  trap - EXIT
  rm -rf -- "$temp_dir"
  if [[ -n ${guest_archive:-} && -n ${CONTROLLER_CTID:-} ]]; then
    pct exec "$CONTROLLER_CTID" -- rm -f -- "$guest_archive" >/dev/null 2>&1 || true
  fi
  if (( result != 0 )); then
    printf 'Bootstrap failed (exit %s). Temporary project removed; LXCs retained for diagnosis.\n' "$result" >&2
    printf 'See /var/log/homelab/jenkins-bootstrap.log and the role installation logs.\n' >&2
  else
    printf 'Temporary project folder removed from Proxmox.\n'
  fi
  exit "$result"
}
trap cleanup EXIT
printf 'Downloading %s/%s (%s) into a temporary folder.\n' "$repo_owner" "$repo_name" "$repo_ref"
archive_url="https://api.github.com/repos/$repo_owner/$repo_name/tarball/$repo_ref"
curl_config="$temp_dir/curl.conf"
: > "$curl_config"
# Optional private-repository access. The token is never a command-line argument.
if [[ -n ${HOMELAB_GITHUB_TOKEN:-} ]]; then
  [[ $HOMELAB_GITHUB_TOKEN =~ ^[A-Za-z0-9_]+$ ]] || { printf 'Invalid token format.\n' >&2; exit 2; }
  printf 'header = "Authorization: Bearer %s"\n' "$HOMELAB_GITHUB_TOKEN" > "$curl_config"
  unset HOMELAB_GITHUB_TOKEN
fi
curl --config "$curl_config" --fail --silent --show-error --location \
  --retry 3 --connect-timeout 15 --max-time 180 \
  "$archive_url" -o "$temp_dir/project.tar.gz"
rm -f -- "$curl_config"
[[ -s $temp_dir/project.tar.gz ]] || { printf 'Repository download was empty.\n' >&2; exit 1; }
# Reject unsafe/unexpected archive paths before extraction.
tar -tzf "$temp_dir/project.tar.gz" > "$temp_dir/archive-members"
archive_root=
while IFS= read -r member; do
  [[ $member != /* && ! $member =~ (^|/)\.\.(/|$) ]] || { printf 'Unsafe archive path.\n' >&2; exit 1; }
  member_root=${member%%/*}
  [[ -n $member_root ]] || exit 1
  if [[ -z $archive_root ]]; then archive_root=$member_root; fi
  [[ $member_root == "$archive_root" ]] || { printf 'Expected one repository root in the archive.\n' >&2; exit 1; }
done < "$temp_dir/archive-members"
mkdir "$temp_dir/project"
tar -xzf "$temp_dir/project.tar.gz" --no-same-owner --strip-components=1 -C "$temp_dir/project"
project_dir="$temp_dir/project"
for file in proxmox_helper_script/create-lxc.sh \
  proxmox_helper_script/lxc/ubuntu/controlplane.profile.sh \
  proxmox_helper_script/lxc/ubuntu/jenkins-agent.profile.sh \
  jenkins/config/install.conf jenkins/deploy/enrol-agent-lxc.sh; do
  [[ -r $project_dir/$file ]] || { printf 'Repository is missing required file: %s\n' "$file" >&2; exit 1; }
done
# Optional trusted host configuration replaces the repository defaults for this run.
if [[ -n ${HOMELAB_INSTALL_CONFIG:-} ]]; then
  [[ -r $HOMELAB_INSTALL_CONFIG ]] || { printf 'Host installation configuration is unreadable.\n' >&2; exit 2; }
  cp -- "$HOMELAB_INSTALL_CONFIG" "$project_dir/jenkins/config/install.conf"
fi
source "$project_dir/jenkins/config/install.conf"
[[ $CONTROLLER_CTID =~ ^[1-9][0-9]+$ && $AGENT_CTID =~ ^[1-9][0-9]+$ && $CONTROLLER_CTID != "$AGENT_CTID" ]] || exit 2

install_role() {
  local profile_name=$1 role=$2 configured_id=$3
  local profile_file="$project_dir/proxmox_helper_script/lxc/ubuntu/$profile_name.profile.sh"
  # Profiles contain trusted Bash; read them in an isolated scope.
  local expected_id expected_hostname expected_mac expected_vlan expected_ipv4_mode expected_ipv4_address expected_tags
  expected_id=$(bash -c 'source "$1"; printf "%s" "$LXC_DEFAULT_CTID"' bash "$profile_file")
  expected_hostname=$(bash -c 'source "$1"; printf "%s" "$LXC_DEFAULT_HOSTNAME"' bash "$profile_file")
  expected_mac=$(bash -c 'source "$1"; printf "%s" "$LXC_DEFAULT_MAC_ADDRESS"' bash "$profile_file")
  expected_vlan=$(bash -c 'source "$1"; printf "%s" "$LXC_DEFAULT_VLAN"' bash "$profile_file")
  expected_ipv4_mode=$(bash -c 'source "$1"; printf "%s" "$LXC_DEFAULT_IPV4_MODE"' bash "$profile_file")
  expected_ipv4_address=$(bash -c 'source "$1"; printf "%s" "$LXC_DEFAULT_IPV4_ADDRESS"' bash "$profile_file")
  expected_tags=$(bash -c 'source "$1"; printf "%s" "$LXC_DEFAULT_TAGS"' bash "$profile_file")
  [[ $configured_id == "$expected_id" ]] || { printf 'Configured CTID differs from %s profile.\n' "$profile_name" >&2; return 2; }
  if pct status "$expected_id" >/dev/null 2>&1; then
    # Verify the project identity and network before offering reuse or deletion.
    local config hostname net tags item key value actual_ipv4
    config=$(pct config "$expected_id")
    hostname=$(printf '%s\n' "$config" | sed -n 's/^hostname: //p')
    net=$(printf '%s\n' "$config" | sed -n 's/^net0: //p')
    tags=$(printf '%s\n' "$config" | sed -n 's/^tags: //p')
    local actual_mac= actual_vlan= actual_net_ipv4=
    IFS=',' read -r -a net_items <<< "$net"
    for item in "${net_items[@]}"; do
      key=${item%%=*}
      value=${item#*=}
      case $key in
        hwaddr) actual_mac=${value,,} ;;
        tag) actual_vlan=$value ;;
        ip) actual_net_ipv4=$value ;;
      esac
    done
    [[ $hostname == "$expected_hostname" && $actual_mac == "${expected_mac,,}" && $actual_vlan == "$expected_vlan" ]] || {
      printf 'CTID %s does not match the expected hostname, MAC, or VLAN; refusing to change it.\n' "$expected_id" >&2
      return 2
    }
    if [[ -n $expected_tags ]]; then
      IFS=';' read -r -a expected_tag_items <<< "$expected_tags"
      for item in "${expected_tag_items[@]}"; do
        [[ ";$tags;" == *";$item;"* ]] || {
          printf 'CTID %s is missing expected tag %s; refusing to change it.\n' "$expected_id" "$item" >&2
          return 2
        }
      done
    fi
    if [[ $expected_ipv4_mode == dhcp ]]; then
      [[ $actual_net_ipv4 == dhcp ]] || {
        printf 'CTID %s does not use DHCP as expected; refusing to change it.\n' "$expected_id" >&2
        return 2
      }
    else
      [[ $actual_net_ipv4 == "$expected_ipv4_address" ]] || {
        printf 'CTID %s IPv4 configuration differs from the profile; refusing to change it.\n' "$expected_id" >&2
        return 2
      }
    fi
    [[ $(pct status "$expected_id") == 'status: running' ]] || pct start "$expected_id"
    actual_ipv4=
    for attempt in {1..30}; do
      actual_ipv4=$(pct exec "$expected_id" -- ip -o -4 addr show scope global 2>/dev/null || true)
      [[ $actual_ipv4 == *"inet $expected_ipv4_address "* ]] && break
      sleep 1
    done
    [[ $actual_ipv4 == *"inet $expected_ipv4_address "* ]] || {
      printf 'CTID %s does not have expected IPv4 address %s; refusing to reinstall.\n' "$expected_id" "$expected_ipv4_address" >&2
      return 2
    }
    local action= choice confirm protection_state delete_script delete_status
    if [[ -r /dev/tty ]]; then
      while true; do
        printf '\n%s (CTID %s) already exists and matches this profile. Choose [r]euse or [d]estroy and recreate: ' "$profile_name" "$expected_id" >/dev/tty
        IFS= read -r choice </dev/tty || return 1
        case ${choice,,} in
          r|reuse) action=reuse; break ;;
          d|destroy) action=destroy; break ;;
          *) printf 'Enter r to reuse or d to destroy and recreate.\n' >/dev/tty ;;
        esac
      done
    else
      action=${HOMELAB_EXISTING_LXC_ACTION:-reuse}
      [[ $action == reuse || $action == destroy ]] || {
        printf 'HOMELAB_EXISTING_LXC_ACTION must be reuse or destroy.\n' >&2
        return 2
      }
      [[ $action == reuse ]] || {
        printf 'Destroying an existing LXC requires an interactive Proxmox terminal.\n' >&2
        return 2
      }
      printf 'No interactive terminal; reusing matching %s (CTID %s).\n' "$profile_name" "$expected_id"
    fi
    if [[ $action == destroy ]]; then
      printf 'The guest-delete helper can delete other guests if selected. In its checklist, select only CTID %s.\n' "$expected_id" >/dev/tty
      printf 'Type DELETE-%s to confirm destroying %s and all data inside it: ' "$expected_id" "$profile_name" >/dev/tty
      IFS= read -r confirm </dev/tty || return 1
      [[ $confirm == "DELETE-$expected_id" ]] || {
        printf 'Deletion cancelled; CTID %s was not changed.\n' "$expected_id" >/dev/tty
        return 1
      }
      command -v whiptail >/dev/null || {
        printf 'whiptail is required by the Community Scripts guest-delete tool; CTID %s was not changed.\n' "$expected_id" >&2
        return 1
      }
      delete_script="$temp_dir/guest-delete.sh"
      curl --fail --silent --show-error --location --retry 3 --connect-timeout 15 --max-time 120 \
        https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/tools/pve/guest-delete.sh \
        -o "$delete_script"
      [[ -s $delete_script ]] || { printf 'Guest-delete download was empty.\n' >&2; return 1; }
      bash -n "$delete_script"
      protection_state=$(printf '%s\n' "$config" | sed -n 's/^protection: //p')
      if [[ $protection_state == 1 || $protection_state == yes ]]; then
        pct set "$expected_id" --protection 0
      fi
      printf '\nStarting Community Scripts guest-delete. Select only LXC CTID %s in its checklist.\n' "$expected_id" >/dev/tty
      if bash "$delete_script" </dev/tty >/dev/tty 2>&1; then
        delete_status=0
      else
        delete_status=$?
      fi
      if pct status "$expected_id" >/dev/null 2>&1; then
        if [[ $protection_state == 1 || $protection_state == yes ]]; then
          pct set "$expected_id" --protection 1 || true
        fi
        printf 'CTID %s still exists (delete helper exit %s); refusing to continue. Protection was restored if it was enabled.\n' "$expected_id" "$delete_status" >&2
        return 1
      fi
      printf 'CTID %s was deleted. Creating a fresh %s.\n' "$expected_id" "$profile_name"
      bash "$project_dir/proxmox_helper_script/create-lxc.sh" "$profile_file"
      return $?
    fi
    printf 'Resuming installation in existing %s (CTID %s).\n' "$profile_name" "$expected_id"
    bash "$project_dir/jenkins/deploy/install-$role-lxc.sh" "$expected_id"
  else
    printf 'Creating %s (CTID %s).\n' "$profile_name" "$expected_id"
    bash "$project_dir/proxmox_helper_script/create-lxc.sh" "$profile_file"
  fi
}
install_role controlplane controller "$CONTROLLER_CTID"
install_role jenkins-agent agent "$AGENT_CTID"

# Keep a repository snapshot on the controlplane after the host temporary copy goes.
# Installed services and their persistent configuration already live in the LXCs.
tar -czf "$temp_dir/installed-project.tar.gz" -C "$project_dir" .
guest_archive=/run/homelab-installed-project.tar.gz
pct push "$CONTROLLER_CTID" "$temp_dir/installed-project.tar.gz" "$guest_archive" --perms 0600
pct exec "$CONTROLLER_CTID" -- install -d -m 0755 /opt/homelab/bootstrap-project
pct exec "$CONTROLLER_CTID" -- tar -xzf "$guest_archive" --no-same-owner -C /opt/homelab/bootstrap-project
pct exec "$CONTROLLER_CTID" -- rm -f -- "$guest_archive"
printf '\nBoth containers are installed. Jenkins: %s\n' "$HOMELAB_JENKINS_URL"
printf 'Controlplane project snapshot: /opt/homelab/bootstrap-project\n'
printf 'Complete the first administrator wizard and supply your private repository credentials.\n'
