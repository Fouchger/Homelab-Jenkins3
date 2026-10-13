output "talos_nodes" {
  description = "VM identities and MAC addresses for MikroTik DHCP reservations and Talos bootstrap inventory."
  value = {
    for name, vm in proxmox_virtual_environment_vm.talos_node : name => {
      vm_id        = vm.vm_id
      proxmox_node = vm.node_name
      mac_address  = vm.mac_addresses[0]
      role         = var.nodes[name].role
      ip_address   = var.nodes[name].ip_address
    }
  }
}

output "kubernetes_api_vip" {
  description = "Candidate static virtual IP for the Talos/Kubernetes API; not provisioned by this OpenTofu environment."
  value       = var.kubernetes_api_vip
}
