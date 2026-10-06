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
    printf 'To retrieve the Jenkins initial admin unlock key, run:\n  pct exec %s -- cat /var/lib/jenkins/secrets/initialAdminPassword\n' "$CONTROLLER_CTID"
  fi
  exit "$result"
}
trap cleanup EXIT
printf 'Downloading %s/%s (%s) into a temporary folder.\n' "$repo_owner" "$repo_name" "$repo_ref"
archive_url="https://api.github.com/repos/$repo_owner/$repo_name/tarball/$repo_ref"
curl_config="$temp_dir/curl.conf"
: > "$curl_config"
# Optional private-repository access. The token is never a command-line argument.
github_token=${HOMELAB_GITHUB_TOKEN:-}
if [[ -n ${HOMELAB_GITHUB_TOKEN_FILE:-} ]]; then
  token_file=$HOMELAB_GITHUB_TOKEN_FILE
  [[ -f $token_file && ! -L $token_file && $(stat -c '%u:%a' "$token_file") == '0:600' ]] || {
    printf 'GitHub token file must be a root-owned regular file with mode 0600.\n' >&2; exit 2;
  }
  IFS= read -r github_token < "$token_file" || true
fi
if [[ -n $github_token ]]; then
  [[ $github_token =~ ^[A-Za-z0-9_]+$ ]] || { printf 'Invalid token format.\n' >&2; exit 2; }
  printf 'header = "Authorization: Bearer %s"\n' "$github_token" > "$curl_config"
fi
unset github_token HOMELAB_GITHUB_TOKEN
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
  proxmox_helper_script/configure-infisical.py \
  proxmox_helper_script/lxc/ubuntu/controlplane.profile.sh \
  proxmox_helper_script/lxc/ubuntu/jenkins-agent.profile.sh \
  jenkins/config/install.conf jenkins/deploy/enrol-agent-lxc.sh \
  jenkins/deploy/import-controller-credentials-lxc.sh; do
  [[ -r $project_dir/$file ]] || { printf 'Repository is missing required file: %s\n' "$file" >&2; exit 1; }
done
# Optional trusted host configuration replaces the repository defaults for this run.
if [[ -n ${HOMELAB_INSTALL_CONFIG:-} ]]; then
  [[ -r $HOMELAB_INSTALL_CONFIG ]] || { printf 'Host installation configuration is unreadable.\n' >&2; exit 2; }
  cp -- "$HOMELAB_INSTALL_CONFIG" "$project_dir/jenkins/config/install.conf"
fi
source "$project_dir/jenkins/config/install.conf"
[[ $CONTROLLER_CTID =~ ^[1-9][0-9]+$ && $AGENT_CTID =~ ^[1-9][0-9]+$ && $CONTROLLER_CTID != "$AGENT_CTID" ]] || exit 2

infisical_setup_dir=
has_interactive_terminal() {
  [[ -t 0 ]] && return 0
  ( : </dev/tty ) >/dev/null 2>&1
}

prompt_whiptail() {
  local kind=$1 title=$2 prompt=$3 default=${4:-} answer
  if [[ $kind == password ]]; then
    answer=$(whiptail --title "$title" --passwordbox "$prompt" 10 78 --output-fd 3 3>&1 1>/dev/tty 2>&1 </dev/tty) || return 1
  else
    answer=$(whiptail --title "$title" --inputbox "$prompt" 10 78 "$default" --output-fd 3 3>&1 1>/dev/tty 2>&1 </dev/tty) || return 1
  fi
  printf '%s' "$answer"
}

write_setup_value() {
  local filename=$1 value=$2
  printf '%s\n' "$value" > "$infisical_setup_dir/$filename"
  chmod 0600 "$infisical_setup_dir/$filename"
}

prepare_infisical_setup() {
  local credential_id infisical_url project_id environment project_slug read_id read_secret write_id write_secret proxmox_host host_key_line host_fingerprint trust_message runtime_config
  [[ -n $infisical_setup_dir ]] && return 0
  has_interactive_terminal || { printf 'An interactive terminal is required to configure Infisical during controlplane setup.\n' >&2; return 1; }
  if ! command -v whiptail >/dev/null; then
    command -v apt-get >/dev/null || { printf 'whiptail is required for Infisical setup, but apt-get is unavailable.\n' >&2; return 1; }
    apt-get install -y whiptail
  fi
  command -v python3 >/dev/null || { printf 'python3 is required for Infisical setup.\n' >&2; return 1; }
  command -v ssh-keygen >/dev/null || { printf 'ssh-keygen is required for Proxmox SSH setup.\n' >&2; return 1; }
  [[ -r /etc/ssh/ssh_host_ed25519_key.pub ]] || { printf 'Cannot read this Proxmox host’s Ed25519 SSH host key.\n' >&2; return 1; }
  infisical_setup_dir="$temp_dir/infisical-setup"
  mkdir -m 0700 "$infisical_setup_dir"
  credential_id=$(prompt_whiptail input 'Infisical setup' 'Jenkins credential ID for the read-only identity:' "${HOMELAB_INFISICAL_CREDENTIAL_ID:-infisical-homelab-prod}") || return 1
  infisical_url=$(prompt_whiptail input 'Infisical setup' 'Infisical HTTPS URL:' "${HOMELAB_INFISICAL_URL:-https://app.infisical.com}") || return 1
  project_id=$(prompt_whiptail input 'Infisical setup' 'Infisical project UUID:' '') || return 1
  environment=$(prompt_whiptail input 'Infisical setup' 'Infisical environment slug:' "${HOMELAB_INFISICAL_ENVIRONMENT:-prod}") || return 1
  project_slug=$(prompt_whiptail input 'Infisical setup' 'Infisical project slug (leave blank if unknown):' "${HOMELAB_INFISICAL_PROJECT_SLUG:-}") || return 1
  read_id=$(prompt_whiptail input 'Infisical setup' 'Client ID for the existing jenkins-read identity:' '') || return 1
  read_secret=$(prompt_whiptail password 'Infisical setup' 'Client Secret for jenkins-read:' '') || return 1
  write_id=$(prompt_whiptail input 'Infisical setup' 'Client ID for the existing jenkins-write identity:' '') || return 1
  write_secret=$(prompt_whiptail password 'Infisical setup' 'Client Secret for jenkins-write:' '') || return 1
  proxmox_host=$(prompt_whiptail input 'Proxmox SSH setup' 'Address Jenkins will use to reach this Proxmox host:' "${HOMELAB_PROXMOX_HOST:-192.168.20.10}") || return 1

  write_setup_value infisical-url "$infisical_url"
  write_setup_value credential-id "$credential_id"
  write_setup_value project-id "$project_id"
  write_setup_value environment "$environment"
  write_setup_value project-slug "$project_slug"
  write_setup_value read-client-id "$read_id"
  write_setup_value read-client-secret "$read_secret"
  write_setup_value write-client-id "$write_id"
  write_setup_value write-client-secret "$write_secret"
  write_setup_value proxmox-host "$proxmox_host"
  unset read_secret write_secret

  ssh-keygen -q -t ed25519 -N '' -C "homelab-jenkins-update-$(date +%s)-$$" -f "$infisical_setup_dir/pve-private-key"
  chmod 0600 "$infisical_setup_dir/pve-private-key" "$infisical_setup_dir/pve-private-key.pub"
  host_key_line=$(awk 'NF >= 2 && $1 == "ssh-ed25519" {print $1 " " $2; exit}' /etc/ssh/ssh_host_ed25519_key.pub)
  [[ -n $host_key_line ]] || { printf 'The local Proxmox Ed25519 host key is invalid.\n' >&2; return 1; }
  printf '%s %s\n' "$proxmox_host" "$host_key_line" > "$infisical_setup_dir/pve-known-hosts"
  cp -- "$infisical_setup_dir/pve-private-key" "$infisical_setup_dir/homelab-pve01-automation-key"
  cp -- "$infisical_setup_dir/pve-private-key.pub" "$infisical_setup_dir/pve-public-key"
  chmod 0600 "$infisical_setup_dir/pve-known-hosts" "$infisical_setup_dir/homelab-pve01-automation-key" "$infisical_setup_dir/pve-public-key"
  host_fingerprint=$(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub)
  printf -v trust_message 'Jenkins will trust the Ed25519 host key read directly from this Proxmox server for %s.\n\n%s\n\nThe key will be saved in Infisical for strict SSH host verification.' "$proxmox_host" "$host_fingerprint"
  if ! whiptail --title 'Trust Proxmox SSH host key' --yesno "$trust_message" 14 78 1>/dev/tty 2>&1 </dev/tty; then
    printf 'Infisical setup cancelled before container creation.\n' >&2
    return 1
  fi
  python3 "$project_dir/proxmox_helper_script/configure-infisical.py" "$infisical_setup_dir" "$proxmox_host"

  cp -- "$infisical_setup_dir/read-client-id" "$infisical_setup_dir/homelab-infisical-client-id"
  cp -- "$infisical_setup_dir/read-client-secret" "$infisical_setup_dir/homelab-infisical-client-secret"
  cp -- "$infisical_setup_dir/write-client-id" "$infisical_setup_dir/homelab-infisical-writer-client-id"
  cp -- "$infisical_setup_dir/write-client-secret" "$infisical_setup_dir/homelab-infisical-writer-client-secret"
  cp -- "$infisical_setup_dir/project-id" "$infisical_setup_dir/homelab-infisical-project-id"
  cp -- "$infisical_setup_dir/infisical-url" "$infisical_setup_dir/homelab-infisical-url"
  cp -- "$infisical_setup_dir/environment" "$infisical_setup_dir/homelab-infisical-environment"
  cp -- "$infisical_setup_dir/project-slug" "$infisical_setup_dir/homelab-infisical-project-slug"
  cp -- "$infisical_setup_dir/proxmox-host" "$infisical_setup_dir/homelab-proxmox-host"
  chmod 0600 "$infisical_setup_dir"/homelab-infisical-*
  chmod 0600 "$infisical_setup_dir/homelab-proxmox-host"
  runtime_config="$infisical_setup_dir/install.conf"
  {
    printf 'source %q\n' "$project_dir/jenkins/config/install.conf"
    printf 'HOMELAB_INFISICAL_CREDENTIAL_ID=%q\n' "$credential_id"
    printf 'HOMELAB_INFISICAL_URL=%q\n' "$infisical_url"
    printf 'HOMELAB_INFISICAL_PROJECT_ID=%q\n' "$project_id"
    printf 'HOMELAB_INFISICAL_ENVIRONMENT=%q\n' "$environment"
    printf 'HOMELAB_INFISICAL_PROJECT_SLUG=%q\n' "$project_slug"
  } > "$runtime_config"
  chmod 0600 "$runtime_config"
  HOMELAB_INSTALL_CONFIG=$runtime_config
  source "$runtime_config"
  export HOMELAB_INSTALL_CONFIG
  HOMELAB_PROXMOX_HOST=$proxmox_host
  export HOMELAB_PROXMOX_HOST
  unset credential_id infisical_url project_id environment project_slug read_id write_id proxmox_host
}

create_lxc_with_password() {
  local profile_file=$1 profile_name=$2 container_id=$3 password confirm password_file infisical_password_file
  has_interactive_terminal || {
    printf 'An interactive terminal is required to set the root password for %s (CTID %s). Refusing to create it without a password.\n' "$profile_name" "$container_id" >&2
    return 2
  }
  command -v whiptail >/dev/null || { printf 'whiptail is required to collect the LXC root password.\n' >&2; return 1; }
  password_file="$temp_dir/root-password.$container_id"
  while true; do
    password=$(prompt_whiptail password 'LXC root password' "Choose a root password for $profile_name (CTID $container_id), at least 12 characters; do not use a colon:") || return 1
    confirm=$(prompt_whiptail password 'Confirm LXC root password' "Confirm the root password for $profile_name (CTID $container_id):") || return 1
    if (( ${#password} < 12 )) || [[ $password == *:* ]]; then
      whiptail --title 'Invalid LXC root password' --msgbox 'Use at least 12 characters and do not include a colon.' 9 70 1>/dev/tty 2>&1 </dev/tty
    elif [[ $password != "$confirm" ]]; then
      whiptail --title 'Password mismatch' --msgbox 'The passwords did not match. Try again.' 9 70 1>/dev/tty 2>&1 </dev/tty
    else
      break
    fi
  done
  (umask 077; printf '%s\n' "$password" > "$password_file")
  unset password confirm
  if [[ -n $infisical_setup_dir ]]; then
    infisical_password_file="$infisical_setup_dir/root-password-$profile_name"
    cp -- "$password_file" "$infisical_password_file"
    chmod 0600 "$infisical_password_file"
    if ! python3 "$project_dir/proxmox_helper_script/configure-infisical.py" --save-lxc-password "$infisical_setup_dir" "$profile_name"; then
      rm -f -- "$password_file" "$infisical_password_file"
      return 1
    fi
    rm -f -- "$infisical_password_file"
  else
    printf 'No Infisical setup was requested this run; the root password will only be applied to the guest.\n'
  fi
  if HOMELAB_LXC_ROOT_PASSWORD_FILE="$password_file" bash "$project_dir/proxmox_helper_script/create-lxc.sh" "$profile_file"; then
    rm -f -- "$password_file"
  else
    local status=$?
    rm -f -- "$password_file"
    return "$status"
  fi
}

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
    local action= choice action_override protection_state delete_status
    if [[ $role == controller ]]; then
      action_override=${HOMELAB_CONTROLPLANE_ACTION:-${HOMELAB_EXISTING_LXC_ACTION:-}}
    else
      action_override=${HOMELAB_AGENT_ACTION:-${HOMELAB_EXISTING_LXC_ACTION:-}}
    fi
    if [[ -n $action_override ]]; then
      action=${action_override,,}
      [[ $action == reuse || $action == destroy ]] || {
        printf '%s action must be reuse or destroy.\n' "$profile_name" >&2
        return 2
      }
    elif has_interactive_terminal; then
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
      action=reuse
      printf 'No interactive terminal; reusing matching %s (CTID %s).\n' "$profile_name" "$expected_id"
    fi
    if [[ $action == destroy ]]; then
      if [[ $role == controller ]]; then
        prepare_infisical_setup || return $?
      elif [[ -z $infisical_setup_dir ]]; then
        printf 'Infisical setup is needed to save the new agent root password.\n'
        prepare_infisical_setup || return $?
      fi
      printf 'Destroying only the verified LXC CTID %s (%s).\n' "$expected_id" "$profile_name"
      protection_state=$(printf '%s\n' "$config" | sed -n 's/^protection: //p')
      if [[ $protection_state == 1 || $protection_state == yes ]]; then
        pct set "$expected_id" --protection 0
      fi
      if [[ $(pct status "$expected_id") == 'status: running' ]]; then
        if ! pct stop "$expected_id"; then
          if [[ $protection_state == 1 || $protection_state == yes ]]; then
            pct set "$expected_id" --protection 1 || true
          fi
          printf 'Could not stop CTID %s; protection was restored if it was enabled.\n' "$expected_id" >&2
          return 1
        fi
      fi
      if pct destroy "$expected_id" -f; then
        delete_status=0
      else
        delete_status=$?
      fi
      if pct status "$expected_id" >/dev/null 2>&1; then
        if [[ $protection_state == 1 || $protection_state == yes ]]; then
          pct set "$expected_id" --protection 1 || true
        fi
        printf 'CTID %s still exists (delete exit %s); refusing to continue. Protection was restored if it was enabled.\n' "$expected_id" "$delete_status" >&2
        return 1
      fi
      printf 'CTID %s was deleted. Creating a fresh %s.\n' "$expected_id" "$profile_name"
      create_lxc_with_password "$profile_file" "$profile_name" "$expected_id"
      return $?
    fi
    if [[ $role == controller ]] && has_interactive_terminal; then
      command -v whiptail >/dev/null || {
        printf 'whiptail is required to choose the Infisical setup action.\n' >&2
        return 1
      }
      local setup_choice
      setup_choice=$(whiptail --title 'Infisical setup' --menu 'Configure or rotate Infisical and Proxmox SSH credentials now?' 12 78 2 \
        configure 'Configure Infisical and generate a new Proxmox SSH key' \
        skip 'Keep the current Infisical and Proxmox SSH credentials' \
        --default-item skip --output-fd 3 3>&1 1>/dev/tty 2>&1 </dev/tty) || return 1
      if [[ $setup_choice == configure ]]; then
        prepare_infisical_setup || return $?
      fi
    fi
    printf 'Resuming installation in existing %s (CTID %s).\n' "$profile_name" "$expected_id"
    bash "$project_dir/jenkins/deploy/install-$role-lxc.sh" "$expected_id"
  else
    printf 'Creating %s (CTID %s).\n' "$profile_name" "$expected_id"
    if [[ $role == controller ]]; then
      prepare_infisical_setup || return $?
    elif [[ -z $infisical_setup_dir ]]; then
      printf 'Infisical setup is needed to save the new agent root password.\n'
      prepare_infisical_setup || return $?
    fi
    create_lxc_with_password "$profile_file" "$profile_name" "$expected_id"
  fi
}
install_role controlplane controller "$CONTROLLER_CTID"
infisical_setup_imported=no
import_infisical_setup() {
  [[ -n $infisical_setup_dir && $infisical_setup_imported == no ]] || return 0
  bash "$project_dir/jenkins/deploy/import-controller-credentials-lxc.sh" "$infisical_setup_dir" "$CONTROLLER_CTID"
  python3 "$project_dir/proxmox_helper_script/configure-infisical.py" --prune-authorized-keys "$infisical_setup_dir"
  infisical_setup_imported=yes
}
import_infisical_setup
install_role jenkins-agent agent "$AGENT_CTID"
import_infisical_setup

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
