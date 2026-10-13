variable "proxmox_bridge" {
  description = "Proxmox Linux bridge carrying the tagged Kubernetes VLAN."
  type        = string
}

variable "kubernetes_vlan_id" {
  description = "Existing MikroTik/Proxmox Kubernetes VLAN ID."
  type        = number
  default     = 80

  validation {
    condition     = var.kubernetes_vlan_id == 80
    error_message = "This environment currently targets the existing VLAN 80-K8S. Change deliberately if the network design changes."
  }
}

variable "kubernetes_api_vip" {
  description = "Reserved Talos/Kubernetes API virtual IP outside the VLAN 80 DHCP pool; verify it is unused before deployment."
  type        = string

  validation {
    condition     = can(regex("^192\\.168\\.80\\.([2-9]|20[0-9]|21[0-9]|22[0-9]|23[0-9]|24[0-9]|25[0-4])$", var.kubernetes_api_vip))
    error_message = "The API VIP must be an unused address in 192.168.80.2-192.168.80.9 or 192.168.80.200-254, outside the DHCP pool 192.168.80.10-199."
  }
}

variable "talos_iso_datastore_id" {
  description = "Proxmox ISO-capable datastore available to the selected VM nodes."
  type        = string
}

variable "talos_iso_proxmox_node" {
  description = "Proxmox node that stores/downloads the Talos installer ISO."
  type        = string
}

variable "talos_iso_file_name" {
  description = "Versioned Talos Image Factory ISO filename."
  type        = string
}

variable "talos_iso_url" {
  description = "Pinned Talos Image Factory ISO URL. Avoid mutable latest URLs."
  type        = string

  validation {
    condition     = can(regex("^https://", var.talos_iso_url))
    error_message = "The Talos ISO must be downloaded over HTTPS."
  }
}

variable "talos_iso_checksum" {
  description = "Expected SHA-256 checksum for the selected Talos ISO."
  type        = string

  validation {
    condition     = can(regex("^[0-9A-Fa-f]{64}$", var.talos_iso_checksum))
    error_message = "talos_iso_checksum must be a 64-character SHA-256 hex digest."
  }
}

variable "cpu_type" {
  description = "Virtual CPU model supported by all selected Proxmox nodes."
  type        = string
  default     = "x86-64-v2-AES"
}

variable "nodes" {
  description = "Talos VMs. Use three control-plane nodes and at least one worker; reserve their DHCP addresses in MikroTik."
  type = map(object({
    role         = string
    vm_id        = number
    proxmox_node = string
    datastore_id = string
    cores        = number
    memory_mb    = number
    disk_gb      = number
    mac_address  = string
    ip_address   = string
  }))

  validation {
    condition = alltrue([
      for node in values(var.nodes) : contains(["controlplane", "worker"], node.role)
    ])
    error_message = "Each node role must be controlplane or worker."
  }

  validation {
    condition = length([
      for node in values(var.nodes) : node if node.role == "controlplane"
    ]) == 3
    error_message = "The selected design requires exactly three control-plane nodes."
  }

  validation {
    condition = length([
      for node in values(var.nodes) : node if node.role == "worker"
    ]) >= 1
    error_message = "Configure at least one worker node."
  }

  validation {
    condition = (
      length(distinct([for node in values(var.nodes) : node.vm_id])) == length(var.nodes) &&
      length(distinct([for node in values(var.nodes) : lower(node.mac_address)])) == length(var.nodes) &&
      length(distinct([for node in values(var.nodes) : node.ip_address])) == length(var.nodes) &&
      !contains([for node in values(var.nodes) : node.ip_address], var.kubernetes_api_vip)
    )
    error_message = "VM IDs, MAC addresses, and node IPs must be unique; the API VIP must not be assigned to a node."
  }

  validation {
    condition = alltrue([
      for node in values(var.nodes) :
      node.vm_id >= 100 &&
      node.vm_id <= 999999999 &&
      node.cores >= 1 &&
      node.memory_mb >= 2048 &&
      node.disk_gb >= 32 &&
      can(regex("^192\\.168\\.80\\.(1[0-9]{2}|[1-9][0-9])$", node.ip_address)) &&
      can(regex("^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$", node.mac_address))
    ])
    error_message = "Every node needs a unique DHCP-pool IP in 192.168.80.10-199, valid MAC and VM ID, at least 1 vCPU, 2 GiB RAM, and 32 GiB disk."
  }
}
