#!/usr/bin/env bash
# Generic Proxmox LXC launcher with verified application bootstrap.
set -Eeuo pipefail

usage() {
  printf 'Usage: %s <profile.sh>\n' "${0##*/}" >&2
  printf 'Example: %s lxc/ubuntu/controlplane.profile.sh\n' "${0##*/}" >&2
}

if [[ $# -ne 1 ]]; then
  usage
  exit 2
fi

profile_file=$1
if [[ ! -f "$profile_file" || ! -r "$profile_file" ]]; then
  printf 'Profile file is missing or unreadable: %s\n' "$profile_file" >&2
  exit 2
fi
profile_file="$(cd -- "$(dirname -- "$profile_file")" && pwd)/$(basename -- "$profile_file")"

# Profile files are trusted Bash configuration files. This preserves arrays and
# computed paths such as LXC_DEFAULT_HOST_POST_INSTALL_SCRIPT.
source "$profile_file"

env_args=()
add_env() {
  local key=$1 value=${2-}
  if [[ -n "$value" ]]; then
    env_args+=("$key=$value")
  fi
  return 0
}
yes_no_to_bit() {
  case "${1,,}" in
    yes|true|1) printf 1 ;;
    no|false|0) printf 0 ;;
    *) printf '%s' "$1" ;;
  esac
}

if [[ -n ${LXC_DEFAULT_OS:-} ]]; then
  add_env var_os "$LXC_DEFAULT_OS"
  add_env var_version "${LXC_DEFAULT_OS_VERSION:-}"
  add_env var_ctid "${LXC_DEFAULT_CTID:-}"
  add_env var_unprivileged "${LXC_DEFAULT_CONTAINER_TYPE:-}"
  add_env var_template_storage "${LXC_DEFAULT_TEMPLATE_STORAGE:-}"
  add_env var_container_storage "${LXC_DEFAULT_CONTAINER_STORAGE:-}"
  add_env var_hostname "${LXC_DEFAULT_HOSTNAME:-}"
  add_env var_disk "${LXC_DEFAULT_DISK_GIB:-}"
  add_env var_cpu "${LXC_DEFAULT_CPU_CORES:-}"
  add_env var_ram "${LXC_DEFAULT_RAM_MIB:-}"
  add_env var_brg "${LXC_DEFAULT_BRIDGE:-}"
  add_env var_sdn_vnet "${LXC_DEFAULT_SDN_VNET:-}"
  case "${LXC_DEFAULT_IPV4_MODE:-dhcp}" in
    dhcp) add_env var_net dhcp ;;
    static) add_env var_net "${LXC_DEFAULT_IPV4_ADDRESS:-}" ;;
    range) add_env var_net "${LXC_DEFAULT_IPV4_RANGE:-}" ;;
    *) printf 'Unsupported IPv4 mode: %s\n' "$LXC_DEFAULT_IPV4_MODE" >&2; exit 2 ;;
  esac
  [[ ${LXC_DEFAULT_IPV4_MODE:-dhcp} == static ]] && add_env var_gateway "${LXC_DEFAULT_GATEWAY:-}"
  add_env var_ipv6_method "${LXC_DEFAULT_IPV6_MODE:-}"
  add_env var_ipv6_static "${LXC_DEFAULT_IPV6_ADDRESS:-}"
  add_env var_mtu "${LXC_DEFAULT_MTU:-}"
  add_env var_searchdomain "${LXC_DEFAULT_SEARCH_DOMAIN:-}"
  add_env var_ns "${LXC_DEFAULT_DNS_SERVER:-}"
  add_env var_mac "${LXC_DEFAULT_MAC_ADDRESS:-}"
  add_env var_vlan "${LXC_DEFAULT_VLAN:-}"
  add_env var_tags "${LXC_DEFAULT_TAGS:-}"
  add_env var_ssh "${LXC_DEFAULT_SSH:-}"
  add_env var_ssh_authorized_key "${LXC_DEFAULT_SSH_KEY:-}"
  add_env var_fuse "${LXC_DEFAULT_FUSE:-}"
  add_env var_tun "${LXC_DEFAULT_TUN:-}"
  add_env var_nesting "$(yes_no_to_bit "${LXC_DEFAULT_NESTING:-}")"
  add_env var_gpu "${LXC_DEFAULT_GPU:-}"
  add_env var_keyctl "$(yes_no_to_bit "${LXC_DEFAULT_KEYCTL:-}")"
  add_env var_apt_cacher "${LXC_DEFAULT_APT_CACHER:-}"
  add_env var_apt_cacher_ip "${LXC_DEFAULT_APT_CACHER_URL:-}"
  [[ ${LXC_DEFAULT_HTTP_PROXY_ENABLED:-no} == yes ]] && add_env var_http_proxy "${LXC_DEFAULT_HTTP_PROXY:-}"
  add_env var_http_no_proxy "${LXC_DEFAULT_NO_PROXY:-}"
  add_env var_inherit_host_ca "${LXC_DEFAULT_INHERIT_HOST_CA:-}"
  add_env var_timezone "${LXC_DEFAULT_TIMEZONE:-}"
  add_env var_protection "${LXC_DEFAULT_PROTECTION:-}"
  add_env var_mknod "$(yes_no_to_bit "${LXC_DEFAULT_MKNOD:-}")"
  add_env var_mount_fs "${LXC_DEFAULT_MOUNT_FS:-}"
  # Application hooks are run by this launcher so failures propagate reliably.
  add_env var_verbose "${LXC_DEFAULT_VERBOSE:-}"
fi

# Also accept the normalized var_* format used by earlier profiles.
if [[ -z ${LXC_DEFAULT_OS:-} ]]; then
  for key in var_os var_version var_ctid var_unprivileged var_template_storage var_container_storage var_hostname var_disk var_cpu var_ram \
    var_brg var_sdn_vnet var_net var_gateway var_ipv6_method var_ipv6_static var_mtu \
    var_searchdomain var_ns var_mac var_vlan var_tags var_ssh var_ssh_authorized_key \
    var_fuse var_tun var_nesting var_gpu var_keyctl var_apt_cacher var_apt_cacher_ip \
    var_http_proxy var_http_no_proxy var_inherit_host_ca var_timezone var_protection \
    var_mknod var_mount_fs var_post_install var_verbose; do
    [[ -v $key ]] && add_env "$key" "${!key}"
  done
fi

# The upstream helper can report successful creation even when its hook fails.
# Own hook execution here, once, and propagate its exit status to callers.
hook_path=${LXC_DEFAULT_HOST_POST_INSTALL_SCRIPT:-${var_post_install:-}}
if [[ -n $hook_path && ! -r $hook_path ]]; then
  printf 'Post-install hook is missing or unreadable: %s\n' "$hook_path" >&2
  exit 2
fi
if [[ -n $hook_path ]]; then
  container_id=${LXC_DEFAULT_CTID:-${var_ctid:-}}
  [[ $EUID -eq 0 ]] || { printf "Run container creation as root on Proxmox.\n" >&2; exit 1; }
  command -v pct >/dev/null || { printf "Proxmox pct is required.\n" >&2; exit 1; }
  [[ $container_id =~ ^[1-9][0-9]+$ ]] || {
    printf 'An explicit CTID is required when using a post-install hook.\n' >&2; exit 2;
  }
  if pct status "$container_id" >/dev/null 2>&1; then
    printf 'CTID %s already exists. Rerun its installation hook instead of creating it again.\n' "$container_id" >&2
    exit 2
  fi
fi
# Remove legacy var_post_install forwarding to prevent double execution.
filtered_env_args=()
for entry in "${env_args[@]}"; do
  [[ $entry == var_post_install=* ]] || filtered_env_args+=("$entry")
done
env_args=("${filtered_env_args[@]}")

if (( ${#env_args[@]} == 0 )); then
  printf 'No installer settings found in %s\n' "$profile_file" >&2
  exit 2
fi

installer_url=${LXC_PROFILE_INSTALLER_URL:-}
if [[ -z "$installer_url" ]]; then
  printf 'Missing LXC_PROFILE_INSTALLER_URL in profile: %s\n' "$profile_file" >&2
  exit 2
fi
if [[ ! "$installer_url" =~ ^https://raw\.githubusercontent\.com/community-scripts/ProxmoxVE/main/ct/[a-z0-9_-]+\.sh$ ]]; then
  printf 'Unsupported installer URL: %s\n' "$installer_url" >&2
  exit 2
fi
printf 'Starting %s installer with profile: %s\n' "${LXC_PROFILE_OS_TITLE:-LXC}" "$profile_file"
temp_dir=$(mktemp -d)
trap 'rm -rf -- "$temp_dir"' EXIT
# A failed/empty download must never become a successful empty bash command.
curl --fail --silent --show-error --location --retry 3 --connect-timeout 15 --max-time 120   "$installer_url" -o "$temp_dir/installer.sh"
[[ -s $temp_dir/installer.sh ]] || { printf 'Installer download was empty.\n' >&2; exit 1; }
bash -n "$temp_dir/installer.sh"
# Explicit empty value suppresses an inherited hook in the installer environment.
env "${env_args[@]}" var_post_install= bash "$temp_dir/installer.sh"
if [[ -n $hook_path ]]; then
  printf 'Container creation completed; running application installation.\n'
  CTID="$container_id" bash "$hook_path"
fi
