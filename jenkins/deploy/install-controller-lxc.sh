#!/usr/bin/env bash
# Jenkins controller installation hook — runs on the Proxmox host.
set -Eeuo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/lxc-install-common.sh"
install_in_lxc controller "${1:-${CTID:-}}"
