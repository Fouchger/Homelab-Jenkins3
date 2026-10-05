#!/usr/bin/env bash
# Create the controlplane LXC and install Jenkins automatically.
set -Eeuo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$script_dir/create-lxc.sh" "$script_dir/lxc/ubuntu/controlplane.profile.sh"
