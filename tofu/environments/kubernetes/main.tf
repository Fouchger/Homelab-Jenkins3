locals {
  talos_iso_file_id = proxmox_download_file.talos_iso.id
}

resource "proxmox_download_file" "talos_iso" {
  content_type       = "iso"
  datastore_id       = var.talos_iso_datastore_id
  file_name          = var.talos_iso_file_name
  node_name          = var.talos_iso_proxmox_node
  url                = var.talos_iso_url
  checksum           = var.talos_iso_checksum
  checksum_algorithm = "sha256"
  overwrite          = false
}

resource "proxmox_virtual_environment_vm" "talos_node" {
  for_each = var.nodes

  name        = each.key
  description = "Talos Kubernetes node managed by OpenTofu"
  node_name   = each.value.proxmox_node
  vm_id       = each.value.vm_id
  on_boot     = true
  started     = true
  protection  = true
  tags        = ["homelab", "kubernetes", "talos", each.value.role]

  # Talos does not run qemu-guest-agent. Do not make Proxmox wait for one.
  agent {
    enabled = false
  }

  cpu {
    cores = each.value.cores
    type  = var.cpu_type
  }

  memory {
    dedicated = each.value.memory_mb
  }

  disk {
    datastore_id = each.value.datastore_id
    interface    = "scsi0"
    size         = each.value.disk_gb
    iothread     = true
  }

  cdrom {
    file_id   = local.talos_iso_file_id
    interface = "ide2"
  }

  network_device {
    bridge      = var.proxmox_bridge
    vlan_id     = var.kubernetes_vlan_id
    mac_address = each.value.mac_address
  }

  operating_system {
    type = "l26"
  }

  scsi_hardware = "virtio-scsi-single"
  boot_order    = ["scsi0", "ide2"]

  serial_device {}

  lifecycle {
    # Rebuilds require an intentional protection change and reviewed plan.
    prevent_destroy = true
  }
}
