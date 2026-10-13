# RouterOS 7.24.5 hAP axÂ² baseline reconciled against the live RTR-MAIN export.
# Snapshot source: homelab-scheduled-sensitive-export.rsc, 2026-10-11 14:51; cross-checked against live WinBox export at 15:03.
# Per user review: VLAN 80 is tagged on bridge and ether2; Kubernetes subnet uses address-list LANS.
# Do not import raw: Jenkins Job 005 renders the protected Infisical values and restores HTTPS/SSH identity.
# The reset wrapper generates rtr-main-https on-router and updates its pin over the preserved SSH host key.
/interface bridge
add comment="Main bridge" frame-types=admit-only-vlan-tagged name=bridge vlan-filtering=yes
/interface vlan
add comment="WAN VLAN" interface=ether1 name=VLAN10-WAN vlan-id=10
add comment="MGMT VLAN" interface=bridge name=VLAN20-MGMT vlan-id=20
add comment="SERVERS VLAN" interface=bridge name=VLAN30-SERVERS vlan-id=30
add comment="USERS VLAN" interface=bridge name=VLAN40-USERS vlan-id=40
add comment="IOT VLAN" interface=bridge name=VLAN50-IOT vlan-id=50
add comment="GUEST VLAN" interface=bridge name=VLAN60-GUEST vlan-id=60
add comment="STORAGE VLAN" interface=bridge name=VLAN70-STORAGE vlan-id=70
add comment="Kubernetes VLAN" interface=bridge name=VLAN80-K8S vlan-id=80
add comment="DMZ VLAN" interface=bridge name=VLAN90-DMZ vlan-id=90
/interface ethernet switch
set switch1 cpu-flow-control=yes
/interface list
add comment="Upstream internet" name=WAN
add comment="Bridge only" name=LAN
add comment="Management VLAN" name=MGMT
add comment="Servers VLAN" name=SERVERS
add comment="Users VLAN" name=USERS
add comment="IoT VLAN" name=IOT
add comment="Guest VLAN" name=GUEST
add comment="Storage VLAN" name=STORAGE
add comment="DMZ VLAN" name=DMZ
add comment="All internal VLAN interfaces (internet-egress rule + future use)" name=VLANS-INTERNAL
/interface wifi channel
add band=2ghz-ax name=ch-24 skip-dfs-channels=all width=20mhz
add band=5ghz-ax name=ch-5 skip-dfs-channels=10min-cac width=20/40/80mhz
/interface wifi datapath
add bridge=bridge name=dp-users vlan-id=40
add bridge=bridge name=dp-mgmt vlan-id=20
add bridge=bridge name=dp-iot vlan-id=50
add bridge=bridge client-isolation=yes disabled=no name=dp-guest vlan-id=60
/interface wifi security
add authentication-types=wpa2-psk,wpa3-psk disabled=no name=sec-users passphrase=${SEC_USERS_PASSWORD}
add authentication-types=wpa2-psk,wpa3-psk disabled=no name=sec-mgmt passphrase=${SEC_MGMT_PASSWORD}
add authentication-types=wpa2-psk,wpa3-psk disabled=no name=sec-iot passphrase=${SEC_IOT_PASSWORD}
add authentication-types=wpa2-psk,wpa3-psk disabled=no name=sec-guest passphrase=${SEC_GUEST_PASSWORD}
/interface wifi configuration
add channel=ch-5 country="New Zealand" datapath=dp-users mode=ap name=cfg-users-5 security=sec-users ssid=Mikrotik-Users
add channel=ch-24 country="New Zealand" datapath=dp-users mode=ap name=cfg-users-24 security=sec-users ssid=Mikrotik-Users
add channel=ch-5 country="New Zealand" datapath=dp-mgmt mode=ap name=cfg-mgmt-5 security=sec-mgmt ssid=Mikrotik-MGMT
add channel=ch-24 country="New Zealand" datapath=dp-mgmt mode=ap name=cfg-mgmt-24 security=sec-mgmt ssid=Mikrotik-MGMT
add channel=ch-5 country="New Zealand" datapath=dp-iot mode=ap name=cfg-iot-5 security=sec-iot ssid=Mikrotik-IoT
add channel=ch-24 country="New Zealand" datapath=dp-iot mode=ap name=cfg-iot-24 security=sec-iot ssid=Mikrotik-IoT
add channel=ch-5 country="New Zealand" datapath=dp-guest mode=ap name=cfg-guest-5 security=sec-guest ssid=Mikrotik-Guest
add channel=ch-24 country="New Zealand" datapath=dp-guest mode=ap name=cfg-guest-24 security=sec-guest ssid=Mikrotik-Guest
/interface wifi
set [ find default-name=wifi1 ] comment="5GHz base radio" configuration=cfg-users-5 configuration.mode=ap disabled=no
add configuration=cfg-guest-5 disabled=no master-interface=wifi1 name=wifi1-guest
add configuration=cfg-iot-5 disabled=no master-interface=wifi1 name=wifi1-iot
add configuration=cfg-mgmt-5 disabled=no master-interface=wifi1 name=wifi1-mgmt
set [ find default-name=wifi2 ] comment="2.4GHz base radio" configuration=cfg-users-24 configuration.mode=ap disabled=no
add configuration=cfg-guest-24 disabled=no master-interface=wifi2 name=wifi2-guest
add configuration=cfg-iot-24 disabled=no master-interface=wifi2 name=wifi2-iot
add configuration=cfg-mgmt-24 disabled=no master-interface=wifi2 name=wifi2-mgmt
/iot lora servers
add address=eu1.cloud.thethings.industries name="TTS Cloud (eu1)" protocol=UDP
add address=nam1.cloud.thethings.industries name="TTS Cloud (nam1)" protocol=UDP
add address=au1.cloud.thethings.industries name="TTS Cloud (au1)" protocol=UDP
add address=eu1.cloud.thethings.network name="TTN V3 (eu1)" protocol=UDP
add address=nam1.cloud.thethings.network name="TTN V3 (nam1)" protocol=UDP
add address=au1.cloud.thethings.network name="TTN V3 (au1)" protocol=UDP
/ip pool
add name=pool-vlan20 ranges=192.168.20.100-192.168.20.199
add name=pool-vlan30 ranges=192.168.30.100-192.168.30.199
add name=pool-vlan40 ranges=192.168.40.100-192.168.40.199
add name=pool-vlan50 ranges=192.168.50.100-192.168.50.199
add name=pool-vlan60 ranges=192.168.60.100-192.168.60.199
add name=pool-vlan70 ranges=192.168.70.100-192.168.70.199
add name=pool-vlan90 ranges=192.168.90.100-192.168.90.199
add name=pool-vlan80 ranges=192.168.80.10-192.168.80.199
/ip dhcp-server
add address-pool=pool-vlan20 authoritative=after-2sec-delay interface=VLAN20-MGMT lease-time=8h name=dhcp-vlan20
add address-pool=pool-vlan30 authoritative=after-2sec-delay interface=VLAN30-SERVERS lease-time=8h name=dhcp-vlan30
add address-pool=pool-vlan40 authoritative=after-2sec-delay interface=VLAN40-USERS lease-time=8h name=dhcp-vlan40
add address-pool=pool-vlan50 authoritative=after-2sec-delay interface=VLAN50-IOT lease-time=8h name=dhcp-vlan50
add address-pool=pool-vlan60 authoritative=after-2sec-delay interface=VLAN60-GUEST lease-time=8h name=dhcp-vlan60
add address-pool=pool-vlan70 authoritative=after-2sec-delay interface=VLAN70-STORAGE lease-time=8h name=dhcp-vlan70
add address-pool=pool-vlan90 authoritative=after-2sec-delay interface=VLAN90-DMZ lease-time=8h name=dhcp-vlan90
add address-pool=pool-vlan80 interface=VLAN80-K8S lease-time=8h name=dhcp-vlan80
/user group
add name=homelab-dns policy="local,telnet,ssh,ftp,reboot,read,write,policy,test,winbox,password,web,sniff,sensitive,api,romon,rest-api"
add name=homelab-backup policy="ssh,ftp,read,write,sensitive,!local,!telnet,!reboot,policy,test,!winbox,!password,!web,!sniff,!api,!romon,!rest-api"
/dude
set enabled=yes
/interface bridge port
add bridge=bridge comment=Trunk frame-types=admit-only-vlan-tagged interface=ether2
add bridge=bridge comment="MGMT access" frame-types=admit-only-untagged-and-priority-tagged interface=ether3 pvid=20
add bridge=bridge comment="MGMT access - pve2" interface=ether4 pvid=20
add bridge=bridge comment="USERS access" frame-types=admit-only-untagged-and-priority-tagged interface=ether5 pvid=40
/ip neighbor discovery-settings
set discover-interface-list=MGMT
/interface bridge vlan
add bridge=bridge comment=SERVERS tagged=bridge,ether2 vlan-ids=30
add bridge=bridge comment=USERS tagged=bridge,ether2,wifi1,wifi2 untagged=ether5 vlan-ids=40
add bridge=bridge comment=STORAGE tagged=bridge,ether2 vlan-ids=70
add bridge=bridge comment=DMZ tagged=bridge,ether2 vlan-ids=90
add bridge=bridge comment=MGMT tagged=bridge,ether2,wifi1-mgmt untagged=ether3,ether4 vlan-ids=20
add bridge=bridge comment="Kubernetes trunk" tagged=bridge,ether2 vlan-ids=80
add bridge=bridge tagged=bridge,ether2,ether4,wifi1-iot vlan-ids=50
/interface list member
add interface=VLAN10-WAN list=WAN
add interface=bridge list=LAN
add interface=VLAN20-MGMT list=MGMT
add interface=VLAN30-SERVERS list=SERVERS
add interface=VLAN40-USERS list=USERS
add interface=VLAN50-IOT list=IOT
add interface=VLAN60-GUEST list=GUEST
add interface=VLAN70-STORAGE list=STORAGE
add interface=VLAN90-DMZ list=DMZ
add interface=VLAN20-MGMT list=VLANS-INTERNAL
add interface=VLAN30-SERVERS list=VLANS-INTERNAL
add interface=VLAN40-USERS list=VLANS-INTERNAL
add interface=VLAN50-IOT list=VLANS-INTERNAL
add interface=VLAN60-GUEST list=VLANS-INTERNAL
add interface=VLAN70-STORAGE list=VLANS-INTERNAL
add interface=VLAN90-DMZ list=VLANS-INTERNAL
add interface=ether1 list=WAN
add interface=VLAN80-K8S list=VLANS-INTERNAL
/ip address
add address=${MIKROTIK_IP}/24 comment="MGMT gateway" interface=VLAN20-MGMT network=192.168.20.0
add address=192.168.30.1/24 comment="SERVERS gateway" interface=VLAN30-SERVERS network=192.168.30.0
add address=192.168.40.1/24 comment="USERS gateway" interface=VLAN40-USERS network=192.168.40.0
add address=192.168.50.1/24 comment="IOT gateway" interface=VLAN50-IOT network=192.168.50.0
add address=192.168.60.1/24 comment="GUEST gateway" interface=VLAN60-GUEST network=192.168.60.0
add address=192.168.70.1/24 comment="STORAGE gateway" interface=VLAN70-STORAGE network=192.168.70.0
add address=192.168.90.1/24 comment="DMZ gateway" interface=VLAN90-DMZ network=192.168.90.0
add address=${MIKROTIK_IP}/24 disabled=yes interface=bridge network=192.168.20.0
add address=${MIKROTIK_IP}/24 disabled=yes interface=bridge network=192.168.20.0
add address=192.168.80.1/24 comment="KUBERNETES gateway" interface=VLAN80-K8S network=192.168.80.0
/ip dhcp-client
add comment="WAN DHCP client" interface=VLAN10-WAN name=client1 use-peer-dns=no use-peer-ntp=no
add disabled=yes interface=ether1 name=client2
/ip dhcp-server lease
add address=192.168.20.10 always-broadcast=yes comment=pve01 mac-address=9C:6B:00:06:49:A7 server=dhcp-vlan20
add address=192.168.20.5 comment=iac-controller mac-address=BC:24:11:4B:E3:DB server=dhcp-vlan20
add address=192.168.20.6 comment=jenkins-agent mac-address=BC:24:11:5E:D9:F9 server=dhcp-vlan20
add address=${DNS01_IPV4} comment=dns01 mac-address=02:00:00:00:01:50 server=dhcp-vlan30
add address=${DNS02_IPV4} comment=dns02 mac-address=02:00:00:00:01:51 server=dhcp-vlan30
/ip dhcp-server network
add address=192.168.20.0/24 comment=MGMT dns-server=${MIKROTIK_IP} domain=fouchger.uk gateway=${MIKROTIK_IP}
add address=192.168.30.0/24 comment=SERVERS dns-server=192.168.30.1 domain=fouchger.uk gateway=192.168.30.1
add address=192.168.40.0/24 comment=USERS dns-server=192.168.40.1 domain=fouchger.uk gateway=192.168.40.1
add address=192.168.50.0/24 comment=IOT dns-server=192.168.50.1 domain=fouchger.uk gateway=192.168.50.1
add address=192.168.60.0/24 comment=GUEST dns-server=192.168.60.1 gateway=192.168.60.1
add address=192.168.70.0/24 comment=STORAGE dns-server=192.168.70.1 domain=fouchger.uk gateway=192.168.70.1
add address=192.168.80.0/24 dns-server=192.168.80.1 domain=fouchger.uk gateway=192.168.80.1
add address=192.168.90.0/24 comment=DMZ dns-server=192.168.90.1 domain=fouchger.uk gateway=192.168.90.1
/ip dns
set allow-remote-requests=yes cache-size=4096KiB servers=${DNS_UPSTREAMS}
/ip dns static
add address=192.168.20.10 comment=homelab-managed-proxmox-api name=pve01.fouchger.uk
/ip firewall address-list
add address=10.0.0.0/8 list=RFC1918
add address=172.16.0.0/12 list=RFC1918
add address=192.168.0.0/16 list=RFC1918
add address=192.168.20.0/24 list=LANS
add address=192.168.30.0/24 list=LANS
add address=192.168.40.0/24 list=LANS
add address=192.168.50.0/24 list=LANS
add address=192.168.60.0/24 list=LANS
add address=192.168.70.0/24 list=LANS
add address=192.168.90.0/24 list=LANS
add address=192.168.80.0/24 comment="Kubernetes subnet for DNS policy" list=LANS
/ip firewall filter
add action=accept chain=input comment=homelab-managed-router-dns-192.168.20.0-24 dst-port=53 protocol=udp src-address=192.168.20.0/24
add action=accept chain=input comment=homelab-managed-router-dns-192.168.20.0-24 dst-port=53 protocol=tcp src-address=192.168.20.0/24
add action=accept chain=input comment=homelab-managed-router-dns-192.168.30.0-24 dst-port=53 protocol=udp src-address=192.168.30.0/24
add action=accept chain=input comment=homelab-managed-router-dns-192.168.30.0-24 dst-port=53 protocol=tcp src-address=192.168.30.0/24
add action=accept chain=input comment=homelab-managed-router-dns-192.168.40.0-24 dst-port=53 protocol=udp src-address=192.168.40.0/24
add action=accept chain=input comment=homelab-managed-router-dns-192.168.40.0-24 dst-port=53 protocol=tcp src-address=192.168.40.0/24
add action=accept chain=input comment=homelab-managed-router-dns-192.168.50.0-24 dst-port=53 protocol=udp src-address=192.168.50.0/24
add action=accept chain=input comment=homelab-managed-router-dns-192.168.50.0-24 dst-port=53 protocol=tcp src-address=192.168.50.0/24
add action=accept chain=input comment=homelab-managed-router-dns-192.168.60.0-24 dst-port=53 protocol=udp src-address=192.168.60.0/24
add action=accept chain=input comment=homelab-managed-router-dns-192.168.60.0-24 dst-port=53 protocol=tcp src-address=192.168.60.0/24
add action=accept chain=input comment=homelab-managed-router-dns-192.168.70.0-24 dst-port=53 protocol=udp src-address=192.168.70.0/24
add action=accept chain=input comment=homelab-managed-router-dns-192.168.70.0-24 dst-port=53 protocol=tcp src-address=192.168.70.0/24
add action=accept chain=input comment=homelab-managed-router-dns-192.168.90.0-24 dst-port=53 protocol=udp src-address=192.168.90.0/24
add action=accept chain=input comment=homelab-managed-router-dns-192.168.90.0-24 dst-port=53 protocol=tcp src-address=192.168.90.0/24
add action=accept chain=input comment="INPUT allow established" connection-state=established,related,untracked
add action=drop chain=input comment="INPUT drop invalid" connection-state=invalid
add action=accept chain=input comment="MGMT SSH Winbox - before brute force" dst-port=22,8291 in-interface-list=MGMT protocol=tcp
add action=accept chain=input comment="MGMT ping - before brute force" in-interface-list=MGMT protocol=icmp
add action=add-src-to-address-list address-list=mgmt_stage1 address-list-timeout=30s chain=input comment="Brute force stage 1" connection-state=new dst-port=22,8291 in-interface-list=WAN protocol=tcp
add action=add-src-to-address-list address-list=mgmt_stage2 address-list-timeout=1m chain=input comment="Brute force stage 2" dst-port=22,8291 in-interface-list=WAN protocol=tcp src-address-list=mgmt_stage1
add action=add-src-to-address-list address-list=mgmt_stage3 address-list-timeout=10m chain=input comment="Brute force stage 3" dst-port=22,8291 in-interface-list=WAN protocol=tcp src-address-list=mgmt_stage2
add action=add-src-to-address-list address-list=mgmt_blacklist address-list-timeout=1d chain=input comment="Brute force blacklist" dst-port=22,8291 in-interface-list=WAN protocol=tcp src-address-list=mgmt_stage3
add action=drop chain=input comment="Drop blacklisted sources" src-address-list=mgmt_blacklist
add action=accept chain=input comment="MGMT DNS DHCP NTP" dst-port=53,67,68,123 in-interface-list=MGMT protocol=udp
add action=accept chain=input comment="MGMT DNS TCP" dst-port=53 in-interface-list=MGMT protocol=tcp
add action=accept chain=input comment="MGMT remaining input" in-interface-list=MGMT
add action=accept chain=input comment="USERS DNS DHCP" dst-port=53,67,68 in-interface-list=USERS protocol=udp
add action=accept chain=input comment="SERVERS DHCP" dst-port=67,68 in-interface-list=SERVERS protocol=udp
add action=accept chain=input comment="DMZ DHCP" dst-port=67,68 in-interface-list=DMZ protocol=udp
add action=accept chain=input comment="STORAGE DHCP" dst-port=67,68 in-interface-list=STORAGE protocol=udp
add action=accept chain=input comment="USERS DNS TCP" dst-port=53 in-interface-list=USERS protocol=tcp
add action=accept chain=input comment="IOT DNS DHCP" dst-port=53,67,68 in-interface-list=IOT protocol=udp
add action=accept chain=input comment="IOT DNS TCP" dst-port=53 in-interface-list=IOT protocol=tcp
add action=accept chain=input comment="GUEST DNS DHCP" dst-port=53,67,68 in-interface-list=GUEST protocol=udp
add action=accept chain=input comment="GUEST DNS TCP" dst-port=53 in-interface-list=GUEST protocol=tcp
add action=drop chain=input comment="Drop WAN to router" in-interface-list=WAN
add action=drop chain=input comment="Drop all remaining input" log=yes log-prefix="fw-in-deny: " src-address=!192.168.80.0/24
add action=accept chain=forward comment="FORWARD allow established" connection-state=established,related,untracked
add action=drop chain=forward comment="FORWARD drop invalid" connection-state=invalid
add action=accept chain=forward comment="Internal VLANs to internet" in-interface-list=VLANS-INTERNAL out-interface-list=WAN
add action=accept chain=forward comment="MGMT to internal VLANs" dst-address-list=LANS src-address=192.168.20.0/24
add action=accept chain=forward comment="DNS to Technitium01 UDP" dst-address=${DNS01_IPV4} dst-port=53 protocol=udp src-address-list=LANS
add action=accept chain=forward comment="DNS to Technitium01 TCP" dst-address=${DNS01_IPV4} dst-port=53 protocol=tcp src-address-list=LANS
add action=accept chain=forward comment="DNS to Technitium02 UDP" dst-address=${DNS02_IPV4} dst-port=53 protocol=udp src-address-list=LANS
add action=accept chain=forward comment="DNS to Technitium02 TCP" dst-address=${DNS02_IPV4} dst-port=53 protocol=tcp src-address-list=LANS
add action=accept chain=forward comment="USERS to SERVERS web" dst-address=192.168.30.0/24 dst-port=80,443 protocol=tcp src-address=192.168.40.0/24
add action=drop chain=forward comment="IOT to SERVERS blocked" dst-address=192.168.30.0/24 src-address=192.168.50.0/24
add action=drop chain=forward comment="DMZ to MGMT blocked" dst-address=192.168.20.0/24 src-address=192.168.90.0/24
add action=drop chain=forward comment="DMZ to STORAGE blocked" dst-address=192.168.70.0/24 src-address=192.168.90.0/24
add action=drop chain=forward comment="Drop all remaining inter-VLAN" dst-address-list=LANS src-address-list=LANS
add action=drop chain=forward comment="FORWARD drop all remaining (default deny)" log=yes log-prefix="fw-fwd-deny: "
add action=accept chain=input comment="KUBERNETES DHCP" dst-port=67,68 in-interface=VLAN80-K8S protocol=udp
add action=accept chain=input comment="KUBERNETES DNS UDP" dst-port=53 protocol=udp src-address=192.168.80.0/24
add action=accept chain=input comment="KUBERNETES DNS TCP" dst-port=53 protocol=tcp src-address=192.168.80.0/24
add action=drop chain=input comment="INPUT drop remaining (final)"
/ip firewall nat
add action=masquerade chain=srcnat comment="Internet NAT" out-interface-list=WAN
/ip service
set ftp disabled=yes
set ssh available-from=192.168.20.0/24
set telnet disabled=yes
set www disabled=yes
set www-ssl available-from=192.168.20.0/24 certificate=rtr-main-https disabled=no
set reverse-proxy disabled=yes
set winbox available-from=192.168.20.0/24
set api available-from=192.168.20.0/24 disabled=yes
set api-ssl disabled=yes
/ipv6 dhcp-client
add add-default-route=yes comment="Contact Broadband IPv6 PD" interface=VLAN10-WAN pool-name=contact-v6-pd pool-prefix-length=64 request=prefix use-peer-dns=no
/ipv6 firewall filter
add action=accept chain=input comment="INPUT allow established and related" connection-state=established,related
add action=drop chain=input comment="INPUT drop invalid" connection-state=invalid
add action=accept chain=input comment="IPv6 ICMPv6 control and diagnostics" protocol=icmpv6
add action=accept chain=input comment="MGMT IPv6 access" in-interface-list=MGMT
add action=drop chain=input comment="Drop IPv6 WAN to router" in-interface-list=WAN
add action=drop chain=input comment="Drop all remaining IPv6 input"
add action=drop chain=forward comment="Drop all remaining IPv6 forward" disabled=yes
add action=accept chain=forward comment="FORWARD allow established and related" connection-state=established,related
add action=drop chain=forward comment="FORWARD drop invalid" connection-state=invalid
add action=accept chain=forward comment="Internal VLANs to IPv6 internet" in-interface-list=VLANS-INTERNAL out-interface-list=WAN
add action=drop chain=forward comment="FORWARD drop all remaining IPv6" disabled=yes
add action=accept chain=forward comment="Internal VLANs to IPv6 internet" disabled=yes in-interface-list=VLANS-INTERNAL out-interface-list=WAN
add action=drop chain=forward comment="IPv6 final forward deny" disabled=yes
add action=accept chain=forward comment="MGMT to internal IPv6 VLANs" in-interface-list=MGMT out-interface-list=VLANS-INTERNAL
add action=drop chain=forward comment="IPv6 final forward deny"
/ipv6 nd
set [ find default=yes ] advertise-dns=yes
/system clock
set time-zone-name=Pacific/Auckland
/system identity
set name=RTR-MAIN
/system logging
add topics=firewall,warning
/system note
set note="Production baseline build - VLAN segmented - managed by homelab MikroTik workflow"
/system ntp client
set enabled=yes
/system ntp client servers
add address=time.cloudflare.com
add address=time.google.com
/system routerboard settings
set auto-upgrade=yes
/system scheduler
add comment="Daily sensitive text export for local router recovery" !days interval=1d name=homelab-sensitive-export on-event="/export show-sensitive file=homelab-scheduled-sensitive-export" policy=ftp,reboot,read,write,policy,test,password,sniff,sensitive,romon start-date=2026-05-21 start-time=03:30:00
add comment="Daily encrypted RouterOS binary backup" !days interval=1d name=homelab-binary-backup on-event="/system backup save name=homelab-scheduled-binary-backup password=\"${BINARY_BACKUP_PASSWORD}\"" policy=ftp,reboot,read,write,policy,test,password,sniff,sensitive,romon start-date=2026-05-21 start-time=03:35:00
/tool mac-server mac-winbox
set allowed-interface-list=none
