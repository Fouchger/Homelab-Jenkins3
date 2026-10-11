# Temporary access bootstrap for a clean MikroTik hAP ax^2 running RouterOS 7.
# Use only on a clean no-defaults router. This is not the full router baseline.
# Connect by MAC WinBox from a laptop on ether3, set the admin password, upload
# this file, then import it. Add the Infisical bootstrap user in WinBox before
# running Job 005. The full baseline in router-baseline.rsc replaces this setup.

/system identity
set name=RTR-MAIN

/interface bridge
add name=bridge protocol-mode=rstp vlan-filtering=no frame-types=admit-only-vlan-tagged

/interface vlan
add comment="Temporary WAN VLAN" interface=ether1 name=VLAN10-WAN vlan-id=10
add comment="Temporary management VLAN" interface=bridge name=VLAN20-MGMT vlan-id=20

/interface list
add name=WAN
add name=MGMT

/interface list member
add interface=VLAN10-WAN list=WAN
add interface=VLAN20-MGMT list=MGMT

/interface bridge port
add bridge=bridge comment="Primary Proxmox tagged trunk" frame-types=admit-only-vlan-tagged ingress-filtering=yes interface=ether2
add bridge=bridge comment="Temporary laptop management access" frame-types=admit-only-untagged-and-priority-tagged ingress-filtering=yes interface=ether3 pvid=20
add bridge=bridge comment="Backup Proxmox management and tagged trunk" frame-types=admit-all ingress-filtering=yes interface=ether4 pvid=20

/interface bridge vlan
add bridge=bridge tagged=bridge,ether2 untagged=ether3,ether4 vlan-ids=20
add bridge=bridge tagged=bridge,ether2,ether4 vlan-ids=30,40,50,60,70,80,90

/ip address
add address=192.168.20.1/24 comment="Temporary management gateway" interface=VLAN20-MGMT network=192.168.20.0

/ip pool
add name=bootstrap-mgmt-pool ranges=192.168.20.100-192.168.20.199

/ip dhcp-server
add address-pool=bootstrap-mgmt-pool authoritative=after-2sec-delay disabled=no interface=VLAN20-MGMT lease-time=1h name=bootstrap-mgmt

/ip dhcp-server network
add address=192.168.20.0/24 dns-server=192.168.20.1 gateway=192.168.20.1

/ip dns
set allow-remote-requests=yes servers=1.1.1.1,1.0.0.1

/ip dhcp-client
add comment="Temporary WAN DHCP client" disabled=no interface=VLAN10-WAN use-peer-dns=no use-peer-ntp=no

/ip firewall filter
add action=accept chain=input comment="Bootstrap established" connection-state=established,related,untracked
add action=drop chain=input comment="Bootstrap invalid input" connection-state=invalid
add action=accept chain=input comment="Bootstrap WAN DHCP reply" in-interface=VLAN10-WAN protocol=udp src-port=67 dst-port=68
add action=accept chain=input comment="Bootstrap management DHCP" in-interface=VLAN20-MGMT protocol=udp dst-port=67
add action=accept chain=input comment="Bootstrap management DNS UDP" in-interface=VLAN20-MGMT protocol=udp dst-port=53
add action=accept chain=input comment="Bootstrap management DNS TCP" in-interface=VLAN20-MGMT protocol=tcp dst-port=53
add action=accept chain=input comment="Bootstrap management access" in-interface=VLAN20-MGMT src-address=192.168.20.0/24
add action=drop chain=input comment="Bootstrap default deny input"
add action=accept chain=forward comment="Bootstrap established" connection-state=established,related,untracked
add action=drop chain=forward comment="Bootstrap invalid forward" connection-state=invalid
add action=accept chain=forward comment="Bootstrap management internet" in-interface=VLAN20-MGMT out-interface-list=WAN
add action=drop chain=forward comment="Bootstrap default deny forward"

/ip firewall nat
add action=masquerade chain=srcnat comment="Temporary management internet" out-interface-list=WAN

/ip service
set ftp disabled=yes
set ssh available-from=192.168.20.0/24 disabled=no
set telnet disabled=yes
set www disabled=yes
set www-ssl port=443 available-from=192.168.20.0/24 disabled=no
set winbox available-from=192.168.20.0/24
set api disabled=yes
set api-ssl disabled=yes

/certificate
add name=bootstrap-https common-name=192.168.20.1 key-usage=tls-server
sign bootstrap-https

/ip service
set www-ssl certificate=bootstrap-https port=443 available-from=192.168.20.0/24 disabled=no

/ip neighbor discovery-settings
set discover-interface-list=MGMT

/system clock
set time-zone-name=Pacific/Auckland

/interface bridge
set bridge vlan-filtering=yes
