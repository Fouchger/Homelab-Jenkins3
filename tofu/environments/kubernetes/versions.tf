terraform {
  required_version = ">= 1.9.0, < 2.0.0"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.114.0"
    }
  }

  backend "local" {
    path = "terraform.tfstate"
  }
}

provider "proxmox" {}
