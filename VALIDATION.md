# Jenkins installation alignment and validation

Baseline: the uploaded Homelab-Jenkins2-main(1).zip, archive commit
`3c2a095b074c824c27c11a307564c59254c88410`.

The guest installer bodies and Jenkins startup hooks were adapted from its
controller/agent installers. Jenkins3 retains its smaller profile-based launcher.

## Alignment

- Java 25 on controller and agent; Jenkins minimum for Java 25 checked.
- Controller Git, jq and plugin stack, zero build executors, inbound node,
  `homelab-automation` label and GitHub-backed verification job.
- Agent OpenTofu, Packer, Task, isolated Ansible Core, proxmoxer, requests,
  and community.proxmox/community.routeros/community.general/kubernetes.core.
- WebSocket systemd service and protected secret handoff through Proxmox.
- Optional credential import hooks with protected host-file staging helper.

## Additional behaviour

Jenkins3 adds role-specific logs, timezone settings, download failure handling,
explicit host hook execution, installation markers and extra agent utilities.
Ansible entry points use executable launcher scripts rather than symlinks.
Configuration defaults point to Homelab-Jenkins3. The verification job is included;
Jenkins2's separate DNS/router/API provisioning workflow is not copied here.

## Checks completed

- 16 isolated integration checks passed, including hook ordering, deferred and
  successful enrolment, secret output protection, invalid-secret rejection,
  failure propagation and credential import file permissions/failures.
- All shell files passed Bash syntax checks.
- The generated configure-jenkins-agent script passed Bash syntax checks.
- Required baseline tools, plugin declarations and collection names were checked.
- Final ZIP integrity check passed.

## Verification still required on Proxmox

Package downloads, Ubuntu template availability, Java startup, Groovy/plugin
API compatibility, first administrator setup and a live WebSocket connection
have not been executed here. Validate the installed node in Jenkins and run
homelab-check after supplying repository access.
