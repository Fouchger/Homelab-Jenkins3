#!/usr/bin/env bash
set -Eeuo pipefail

# Homelab control plane LXC defaults.
# Configure the MikroTik reservation to assign 192.168.20.5 to this MAC first.

installer_url="https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/ct/ubuntu.sh"
temp_dir="$(mktemp -d)"
cleanup() {
  rm -f -- "$temp_dir/ubuntu.sh"
  rmdir -- "$temp_dir" 2>/dev/null || true
}
trap cleanup EXIT

curl -fsSL "$installer_url" -o "$temp_dir/ubuntu.sh"

env \
  var_os=ubuntu \
  var_version=26.04 \
  var_ctid=100 \
  var_unprivileged=1 \
  var_hostname=controlplane \
  var_disk=50 \
  var_cpu=2 \
  var_ram=4096 \
  var_brg=vmbr0 \
  var_net=dhcp \
  var_ipv6_method=auto \
  var_mac=BC:24:11:4B:E3:DB \
  var_vlan=20 \
  'var_tags=homelab;controlplane;iac' \
  var_ssh=yes \
  var_fuse=no \
  var_tun=no \
  var_nesting=1 \
  var_gpu=no \
  var_keyctl=1 \
  var_protection=yes \
  var_mknod=no \
  var_apt_cacher=no \
  var_http_no_proxy=localhost,127.0.0.1,.local \
  bash "$temp_dir/ubuntu.sh"
