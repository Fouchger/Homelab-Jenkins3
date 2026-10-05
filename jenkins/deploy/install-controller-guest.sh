#!/usr/bin/env bash
# Install Jenkins LTS and its Java runtime inside an Ubuntu LXC.
set -Eeuo pipefail

INSTALL_ROLE=controller
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/guest-common.sh"
JENKINS_PORT=${JENKINS_PORT:-8080}
[[ $JENKINS_PORT =~ ^[0-9]+$ && ${#JENKINS_PORT} -le 5 ]] || exit 2
(( 10#$JENKINS_PORT >= 1024 && 10#$JENKINS_PORT <= 65535 )) || exit 2
JENKINS_PORT=$((10#$JENKINS_PORT))
# Keep an existing runtime selected while Java 25 is installed and Jenkins is
# upgraded. The explicit switch happens only after the Jenkins version check.
if command -v java >/dev/null 2>&1; then
  current_java_path="$(readlink -f "$(command -v java)")"
  update-alternatives --set java "$current_java_path"
fi
apt-get install -y ca-certificates curl fontconfig jq openssh-client python3 openjdk-25-jre

install -d -m 0755 /etc/apt/keyrings
key_tmp="$(mktemp)"
trap 'rm -f -- "$key_tmp"' EXIT
curl --fail --silent --show-error --location --retry 3 --connect-timeout 15 --max-time 120 \
  https://pkg.jenkins.io/debian-stable/jenkins.io-2026.key -o "$key_tmp"
fingerprint=$(gpg --batch --show-keys --with-colons "$key_tmp" | awk -F: '$1 == "fpr" {print $10; exit}')
[[ $fingerprint == 5E386EADB55F01504CAE8BCF7198F4B714ABFC68 ]] || {
  printf 'Unexpected Jenkins signing key; installation stopped.\n' >&2; exit 1;
}
install -m 0644 "$key_tmp" /etc/apt/keyrings/jenkins-keyring.asc
rm -f -- "$key_tmp"
trap - EXIT

cat >/etc/apt/sources.list.d/jenkins.list <<'JENKINS_REPO'
deb [signed-by=/etc/apt/keyrings/jenkins-keyring.asc] https://pkg.jenkins.io/debian-stable binary/
JENKINS_REPO

apt-get update
apt-get install -y git
if [[ -n ${JENKINS_VERSION:-} ]]; then
  apt_install "jenkins=$JENKINS_VERSION"
else
  apt_install jenkins
fi

# Jenkins supports Java 25 starting with LTS 2.541.1. Keep Java 21 available
# for rollback, but make the runtime used by Jenkins explicitly Java 25.
jenkins_version="$(dpkg-query -W -f='${Version}' jenkins)"
if ! dpkg --compare-versions "$jenkins_version" ge '2.541.1'; then
  echo "Jenkins $jenkins_version is too old for Java 25; refusing to switch runtimes." >&2
  exit 1
fi
java25_path="$(update-alternatives --list java | awk '/java-25-openjdk/ { print; exit }')"
[[ -x "$java25_path" ]] || { echo "OpenJDK 25 is installed but its java alternative is missing." >&2; exit 1; }
update-alternatives --set java "$java25_path"
java -version

jenkins_home=/var/lib/jenkins
settings_dir=/etc/homelab
install -d -m 0755 "$settings_dir"
cat >"$settings_dir/project.properties" <<PROJECT_SETTINGS
githubOwner=${HOMELAB_GITHUB_OWNER:-Fouchger}
githubRepository=${HOMELAB_GITHUB_REPOSITORY:-Homelab-Jenkins3}
githubBranch=${HOMELAB_GITHUB_BRANCH:-main}
githubCredentialId=${HOMELAB_GITHUB_CREDENTIAL_ID:-github-homelab-jenkins-readonly}
infisicalReadCredentialId=${HOMELAB_INFISICAL_CREDENTIAL_ID:-infisical-homelab-prod}
infisicalUrl=${HOMELAB_INFISICAL_URL:-https://app.infisical.com}
infisicalProjectId=${HOMELAB_INFISICAL_PROJECT_ID:-}
infisicalEnvironment=${HOMELAB_INFISICAL_ENVIRONMENT:-prod}
infisicalProjectSlug=${HOMELAB_INFISICAL_PROJECT_SLUG:-}
PROJECT_SETTINGS
chmod 0644 "$settings_dir/project.properties"
plugin_dir="$jenkins_home/plugins"
install -d -o jenkins -g jenkins -m 0750 "$plugin_dir"

# Install the Git, Pipeline, SSH credential, binding, Infisical, and extended
# timer trigger plugins before Jenkins starts so seeded jobs are ready.
plugins_present=yes
for plugin_name in git workflow-job workflow-cps workflow-scm-step ssh-credentials credentials-binding infisical extended-timer-trigger; do
  if [[ ! -s "$plugin_dir/$plugin_name.jpi" && ! -s "$plugin_dir/$plugin_name.hpi" ]]; then
    plugins_present=no
    break
  fi
done
if [[ "$plugins_present" == no ]]; then
  plugin_stage="$(mktemp -d /tmp/jenkins-plugin-stage.XXXXXX)"
  trap 'rm -rf -- "$plugin_stage"' EXIT
  plugin_manager_jar="$plugin_stage/jenkins-plugin-manager.jar"
  plugin_manager_url="$(curl --fail --silent --show-error --location \
    https://api.github.com/repos/jenkinsci/plugin-installation-manager-tool/releases/latest \
    | jq -r '[.assets[] | select(.name | startswith("jenkins-plugin-manager-") and endswith(".jar")) | .browser_download_url] | first // empty')"
  [[ -n "$plugin_manager_url" ]] || { echo "Could not locate the Jenkins plugin installation manager release." >&2; exit 1; }
  curl --fail --silent --show-error --location "$plugin_manager_url" --output "$plugin_manager_jar"
  java -jar "$plugin_manager_jar" \
    --war /usr/share/java/jenkins.war \
    --plugin-download-directory "$plugin_stage/plugins" \
    --plugins git workflow-aggregator ssh-credentials credentials-binding infisical extended-timer-trigger
  find "$plugin_stage/plugins" -maxdepth 1 -type f \( -name '*.jpi' -o -name '*.hpi' \) \
    -exec install -o jenkins -g jenkins -m 0644 {} "$plugin_dir/" \;
  rm -rf -- "$plugin_stage"
  trap - EXIT
fi

# Create the inbound automation node from a core Jenkins startup hook. This
# works before the first-login wizard is completed and needs no Jenkins API token.
install -d -o jenkins -g jenkins -m 0750 "$jenkins_home/init.groovy.d"
install -d -o jenkins -g jenkins -m 0700 "$jenkins_home/secrets"
cat >"$jenkins_home/init.groovy.d/90-homelab-automation-agent.groovy" <<'JENKINS_AGENT_HOOK'
import hudson.model.Node
import hudson.slaves.DumbSlave
import hudson.slaves.JNLPLauncher
import hudson.slaves.RetentionStrategy
import jenkins.model.Jenkins

def jenkins = Jenkins.get()
def agentName = 'jenkins-agent'
def agent = jenkins.getNode(agentName)

// Keep builds off the controller; homelab pipelines must target the agent label.
jenkins.setNumExecutors(0)
jenkins.save()

if (agent == null) {
    agent = new DumbSlave(agentName, '/var/lib/jenkins-agent', new JNLPLauncher())
    jenkins.addNode(agent)
}
if (!(agent instanceof DumbSlave)) {
    throw new IllegalStateException("Jenkins node '${agentName}' exists but is not a permanent agent")
}

agent.setNodeDescription('Homelab automation worker')
agent.setNumExecutors(1)
agent.setMode(Node.Mode.EXCLUSIVE)
agent.setLabelString('homelab-automation')
agent.setLauncher(new JNLPLauncher())
agent.setRetentionStrategy(new RetentionStrategy.Always())
agent.save()
jenkins.save()

def computer = agent.toComputer()
if (computer == null || !computer.getJnlpMac()) {
    throw new IllegalStateException("Jenkins could not create the inbound secret for '${agentName}'")
}

def pendingFile = new File('/var/lib/jenkins/secrets/homelab-agent-enrollment.pending')
if (!pendingFile.exists()) {
    println("Jenkins inbound node '${agentName}' is configured; no secret handoff is pending.")
    return
}

def secretFile = new File('/var/lib/jenkins/secrets/homelab-agent-bootstrap.secret')
secretFile.setText(computer.getJnlpMac() + System.lineSeparator(), 'UTF-8')
secretFile.setReadable(false, false)
secretFile.setWritable(false, false)
secretFile.setReadable(true, true)
secretFile.setWritable(true, true)
if (!pendingFile.delete()) {
    throw new IllegalStateException('Could not clear the pending agent enrollment marker')
}
println("Configured Jenkins inbound node '${agentName}' with label 'homelab-automation'.")
JENKINS_AGENT_HOOK
chown jenkins:jenkins "$jenkins_home/init.groovy.d/90-homelab-automation-agent.groovy"
chmod 0640 "$jenkins_home/init.groovy.d/90-homelab-automation-agent.groovy"

# Import a one-time Proxmox SSH key into Jenkins' encrypted credentials store.
# The host bootstrap supplies this protected file only when the credential is absent.
cat >"$jenkins_home/init.groovy.d/93-homelab-pve-ssh-credential.groovy" <<'JENKINS_PVE_SSH_CREDENTIAL_HOOK'
import com.cloudbees.jenkins.plugins.sshcredentials.impl.BasicSSHUserPrivateKey
import com.cloudbees.plugins.credentials.CredentialsScope
import com.cloudbees.plugins.credentials.SystemCredentialsProvider
import com.cloudbees.plugins.credentials.domains.Domain
import com.cloudbees.plugins.credentials.impl.UsernamePasswordCredentialsImpl

def keyFile = new File('/run/homelab-pve01-automation-key')
if (!keyFile.isFile()) {
    return
}

try {
    def privateKey = keyFile.getText('UTF-8').trim()
    if (!privateKey.startsWith('-----BEGIN OPENSSH PRIVATE KEY-----')) {
        throw new IllegalArgumentException('Expected an OpenSSH Ed25519 private key.')
    }

    def credentialId = 'pve01-automation-ssh'
    def provider = SystemCredentialsProvider.getInstance()
    def credential = new BasicSSHUserPrivateKey(
        CredentialsScope.GLOBAL,
        credentialId,
        'root',
        new BasicSSHUserPrivateKey.DirectEntryPrivateKeySource(privateKey),
        '',
        'Restricted Jenkins automation access to pve01 from the homelab agent'
    )
    def domainCredentials = new LinkedHashMap(provider.getDomainCredentialsMap())
    def globalCredentials = new ArrayList(domainCredentials.get(Domain.global()) ?: [])
    globalCredentials.removeAll { it.id == credentialId }
    globalCredentials.add(credential)
    domainCredentials.put(Domain.global(), globalCredentials)
    provider.setDomainCredentialsMap(domainCredentials)
    provider.save()
    println("Configured Jenkins SSH credential '${credentialId}'.")
} finally {
    keyFile.delete()
}
JENKINS_PVE_SSH_CREDENTIAL_HOOK
chown jenkins:jenkins "$jenkins_home/init.groovy.d/93-homelab-pve-ssh-credential.groovy"
chmod 0640 "$jenkins_home/init.groovy.d/93-homelab-pve-ssh-credential.groovy"
# Import the private GitHub token from a short-lived file supplied by the
# Proxmox bootstrap. Jenkins encrypts it in its own credentials store.
cat >"$jenkins_home/init.groovy.d/94-homelab-github-credential.groovy" <<'JENKINS_CREDENTIAL_HOOK'
import com.cloudbees.plugins.credentials.CredentialsScope
import com.cloudbees.plugins.credentials.SystemCredentialsProvider
import com.cloudbees.plugins.credentials.domains.Domain
import com.cloudbees.plugins.credentials.impl.UsernamePasswordCredentialsImpl

def tokenFile = new File('/run/homelab-github-readonly.token')
if (!tokenFile.isFile()) {
    return
}

try {
    def token = tokenFile.getText('UTF-8').trim()
    if (!(token ==~ /^github_pat_[A-Za-z0-9_]{20,}$/)) {
        throw new IllegalArgumentException('Expected a GitHub fine-grained personal access token.')
    }

    def projectSettings = new Properties()
    new File('/etc/homelab/project.properties').withInputStream { projectSettings.load(it) }
    def credentialId = projectSettings.getProperty('githubCredentialId', 'github-homelab-jenkins-readonly')
    def provider = SystemCredentialsProvider.getInstance()
    def credential = new UsernamePasswordCredentialsImpl(
        CredentialsScope.GLOBAL,
        credentialId,
        'Read-only GitHub access for Homelab-Jenkins',
        projectSettings.getProperty('githubOwner', 'Fouchger'),
        token
    )
    def domainCredentials = new LinkedHashMap(provider.getDomainCredentialsMap())
    def globalCredentials = new ArrayList(domainCredentials.get(Domain.global()) ?: [])
    globalCredentials.removeAll { it.id == credentialId }
    globalCredentials.add(credential)
    domainCredentials.put(Domain.global(), globalCredentials)
    provider.setDomainCredentialsMap(domainCredentials)
    provider.save()
    println("Configured Jenkins credential '${credentialId}' for the private homelab repository.")
} finally {
    tokenFile.delete()
}
JENKINS_CREDENTIAL_HOOK
chown jenkins:jenkins "$jenkins_home/init.groovy.d/94-homelab-github-credential.groovy"
chmod 0640 "$jenkins_home/init.groovy.d/94-homelab-github-credential.groovy"

# Import read-only and writer Universal Auth pairs from one-time protected
# files supplied by Proxmox bootstrap. Jenkins encrypts them in its credential
# store, removes the temporary files, and reports authentication status for reuse.
cat >"$jenkins_home/init.groovy.d/94-homelab-infisical-credential.groovy" <<'JENKINS_INFISICAL_CREDENTIAL_HOOK'
import com.cloudbees.plugins.credentials.CredentialsScope
import com.cloudbees.plugins.credentials.SystemCredentialsProvider
import com.cloudbees.plugins.credentials.domains.Domain
import io.jenkins.plugins.infisicaljenkins.configuration.InfisicalConfiguration
import io.jenkins.plugins.infisicaljenkins.credentials.InfisicalUniversalAuthCredential
import com.cloudbees.plugins.credentials.impl.UsernamePasswordCredentialsImpl

def projectSettings = new Properties()
new File('/etc/homelab/project.properties').withInputStream { projectSettings.load(it) }
def identities = [
    [id: projectSettings.getProperty('infisicalReadCredentialId', 'infisical-homelab-prod'), description: 'Infisical read-only Universal Auth for Homelab Jenkins Production',
     clientIdPath: '/run/homelab-infisical-client-id', secretPath: '/run/homelab-infisical-client-secret', statusName: 'read'],
    [id: 'infisical-homelab-prod-writer', description: 'Infisical writer Universal Auth for Homelab Jenkins Production',
     clientIdPath: '/run/homelab-infisical-writer-client-id', secretPath: '/run/homelab-infisical-writer-client-secret', statusName: 'writer']
]

identities.each { identity ->
    def clientIdFile = new File(identity.clientIdPath)
    def secretFile = new File(identity.secretPath)
    if (!clientIdFile.isFile() || !secretFile.isFile()) {
        return
    }

    try {
        def clientId = clientIdFile.getText('UTF-8').trim()
        def clientSecret = secretFile.getText('UTF-8').trim()
        if (!(clientId ==~ /^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$/)) {
            throw new IllegalArgumentException("Infisical Client ID for ${identity.id} must be a UUID.")
        }
        if (clientSecret.length() < 16) {
            throw new IllegalArgumentException("Infisical Client Secret for ${identity.id} is empty or unexpectedly short.")
        }

        def credential = new InfisicalUniversalAuthCredential(
            CredentialsScope.GLOBAL, identity.id, identity.description, clientId, clientSecret
        )
        def provider = SystemCredentialsProvider.getInstance()
        def domainCredentials = new LinkedHashMap(provider.getDomainCredentialsMap())
        def globalCredentials = new ArrayList(domainCredentials.get(Domain.global()) ?: [])
        globalCredentials.removeAll { it.id == identity.id }
        globalCredentials.add(credential)
        def apiCredentialId = identity.statusName == 'writer'
            ? 'infisical-homelab-prod-writer-api'
            : 'infisical-homelab-prod-read-api'
        globalCredentials.removeAll { it.id == apiCredentialId }
        globalCredentials.add(new UsernamePasswordCredentialsImpl(
            CredentialsScope.GLOBAL, apiCredentialId,
            "Infisical ${identity.statusName} Universal Auth for API calls from Jenkins pipelines",
            clientId, clientSecret
        ))
        domainCredentials.put(Domain.global(), globalCredentials)
        provider.setDomainCredentialsMap(domainCredentials)
        provider.save()
        println("Configured Jenkins Infisical Universal Auth credential '${identity.id}'.")
    } finally {
        clientIdFile.delete()
        secretFile.delete()
    }
}

def infisicalConfig = new InfisicalConfiguration()
infisicalConfig.setInfisicalUrl(projectSettings.getProperty('infisicalUrl', 'https://app.infisical.com'))
def savedCredentials = SystemCredentialsProvider.getInstance().getDomainCredentialsMap().get(Domain.global()) ?: []
identities.each { identity ->
    def statusFile = new File("/var/lib/jenkins/secrets/homelab-infisical-${identity.statusName}.status")
    def credential = savedCredentials.find { it.id == identity.id }
    if (credential == null) {
        statusFile.text = 'missing\n'
        return
    }
    try {
        def accessToken = credential.getAccessToken(infisicalConfig)
        if (accessToken == null || accessToken.toString().isEmpty()) {
            throw new IllegalStateException('Infisical did not return an access token.')
        }
        statusFile.text = 'valid\n'
    } catch (Exception ignored) {
        statusFile.text = 'invalid\n'
    }
}
JENKINS_INFISICAL_CREDENTIAL_HOOK
chown jenkins:jenkins "$jenkins_home/init.groovy.d/94-homelab-infisical-credential.groovy"
chmod 0640 "$jenkins_home/init.groovy.d/94-homelab-infisical-credential.groovy"
rm -f -- "$jenkins_home/secrets/homelab-infisical-read.status" "$jenkins_home/secrets/homelab-infisical-writer.status"

# Seed the private GitHub Pipeline item on a new controller. The preceding
# startup hook imports the bootstrap-supplied credential; no token is in this repo.
cat >"$jenkins_home/init.groovy.d/95-homelab-github-pipeline.groovy" <<'JENKINS_PIPELINE_HOOK'
import hudson.plugins.git.BranchSpec
import hudson.plugins.git.GitSCM
import hudson.triggers.SCMTrigger
import jenkins.model.Jenkins
import org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition
import org.jenkinsci.plugins.workflow.job.WorkflowJob

def jenkins = Jenkins.get()
def projectSettings = new Properties()
new File('/etc/homelab/project.properties').withInputStream { projectSettings.load(it) }
def repositoryUrl = "https://github.com/${projectSettings.getProperty('githubOwner')}/${projectSettings.getProperty('githubRepository')}.git"
def credentialId = projectSettings.getProperty('githubCredentialId', 'github-homelab-jenkins-readonly')
def branch = projectSettings.getProperty('githubBranch', 'main')
def jobName = 'homelab-check'
def job = jenkins.getItem(jobName)
def scm = new GitSCM(
    GitSCM.createRepoList(repositoryUrl, credentialId),
    Collections.singletonList(new BranchSpec("*/${branch}")),
    null,
    null,
    Collections.emptyList()
)
def definition = new CpsScmFlowDefinition(scm, 'jenkins/pipelines/toolchain-check.Jenkinsfile')
definition.setLightweight(true)
if (job == null) {
    job = jenkins.createProject(WorkflowJob, jobName)
    job.setDescription('Read-only homelab worker checks from the private GitHub repository.')
    job.addTrigger(new SCMTrigger('H/5 * * * *'))
    println("Created GitHub-backed Pipeline '${jobName}' for branch '${branch}'.")
} else if (!(job instanceof WorkflowJob)) {
    throw new IllegalStateException("Jenkins item '${jobName}' exists but is not a Pipeline job")
} else {
    println("Updating managed Pipeline '${jobName}' to use the repository's Jenkins pipeline folder.")
}
job.setDefinition(definition)
job.setDescription('AUTOMATIC CHECK every 5 minutes: confirms the private repository can be read and the Jenkins agent/toolchain are available. It does not configure or change homelab services; normally do not start it manually.')
job.save()
JENKINS_PIPELINE_HOOK
chown jenkins:jenkins "$jenkins_home/init.groovy.d/95-homelab-github-pipeline.groovy"
chmod 0640 "$jenkins_home/init.groovy.d/95-homelab-github-pipeline.groovy"

# Seed the manual Proxmox access setup pipeline. It rotates a token only when
# explicitly started by an operator and uses the existing GitHub SCM credential.
cat >"$jenkins_home/init.groovy.d/96-homelab-proxmox-access-pipeline.groovy" <<'JENKINS_PVE_PIPELINE_HOOK'
import hudson.plugins.git.BranchSpec
import hudson.plugins.git.GitSCM
import jenkins.model.Jenkins
import org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition
import org.jenkinsci.plugins.workflow.job.WorkflowJob

def jenkins = Jenkins.get()
def projectSettings = new Properties()
new File('/etc/homelab/project.properties').withInputStream { projectSettings.load(it) }
def repositoryUrl = "https://github.com/${projectSettings.getProperty('githubOwner')}/${projectSettings.getProperty('githubRepository')}.git"
def credentialId = projectSettings.getProperty('githubCredentialId', 'github-homelab-jenkins-readonly')
def branch = projectSettings.getProperty('githubBranch', 'main')
def jobName = '001 - Proxmox Access Setup'
def job = jenkins.getItem(jobName)
def scm = new GitSCM(
    GitSCM.createRepoList(repositoryUrl, credentialId),
    Collections.singletonList(new BranchSpec("*/${branch}")),
    null, null, Collections.emptyList()
)
def definition = new CpsScmFlowDefinition(scm, 'jenkins/pipelines/proxmox-access/Jenkinsfile')
definition.setLightweight(true)
if (job == null) {
    job = jenkins.createProject(WorkflowJob, jobName)
    println("Created manual Pipeline '${jobName}' for branch '${branch}'.")
} else if (!(job instanceof WorkflowJob)) {
    throw new IllegalStateException("Jenkins item '${jobName}' exists but is not a Pipeline job")
} else {
    println("Updating managed Pipeline '${jobName}' to use the repository's Jenkins pipeline folder.")
}
job.setDefinition(definition)
job.setDescription('001 - Applies the existing HomelabLxcOperator role to the Proxmox automation account and selected guest/storage paths, creates or rotates its API token, updates /proxmox/automation in Infisical, verifies the saved values, then removes the prior token when it belongs to this account. Start manually when needed.')
job.save()
JENKINS_PVE_PIPELINE_HOOK
chown jenkins:jenkins "$jenkins_home/init.groovy.d/96-homelab-proxmox-access-pipeline.groovy"
chmod 0640 "$jenkins_home/init.groovy.d/96-homelab-proxmox-access-pipeline.groovy"

# Seed the manual full-stack update job. It always reuses the two verified LXCs.
cat >"$jenkins_home/init.groovy.d/97-homelab-server-update-pipeline.groovy" <<'JENKINS_UPDATE_PIPELINE_HOOK'
import hudson.model.BooleanParameterDefinition
import hudson.model.ParametersDefinitionProperty
import hudson.plugins.git.BranchSpec
import hudson.plugins.git.GitSCM
import io.jenkins.plugins.extended_timer_trigger.ExtendedTimerTrigger
import jenkins.model.Jenkins
import org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition
import org.jenkinsci.plugins.workflow.job.WorkflowJob

def jenkins = Jenkins.get()
def projectSettings = new Properties()
new File('/etc/homelab/project.properties').withInputStream { projectSettings.load(it) }
def repositoryUrl = "https://github.com/${projectSettings.getProperty('githubOwner')}/${projectSettings.getProperty('githubRepository')}.git"
def credentialId = projectSettings.getProperty('githubCredentialId', 'github-homelab-jenkins-readonly')
def branch = projectSettings.getProperty('githubBranch', 'main')
def jobName = '002 - Update Servers'
def job = jenkins.getItem(jobName)
def scm = new GitSCM(
    GitSCM.createRepoList(repositoryUrl, credentialId),
    Collections.singletonList(new BranchSpec("*/${branch}")),
    null, null, Collections.emptyList()
)
def definition = new CpsScmFlowDefinition(scm, 'jenkins/pipelines/server-update/Jenkinsfile')
definition.setLightweight(true)
if (job == null) {
    job = jenkins.createProject(WorkflowJob, jobName)
    println("Created manual Pipeline '${jobName}' for branch '${branch}'.")
} else if (!(job instanceof WorkflowJob)) {
    throw new IllegalStateException("Jenkins item '${jobName}' exists but is not a Pipeline job")
} else {
    println("Updating managed Pipeline '${jobName}' to the current repository pipeline path.")
}
job.setDefinition(definition)
job.setDescription('002 - Updates both verified Jenkins LXCs from the configured GitHub branch every day at 2:00 a.m. Pacific/Auckland. Timer runs proceed automatically; manual runs require confirmation. Containers are always reused and never destroyed.')
job.removeProperty(ParametersDefinitionProperty)
job.addProperty(new ParametersDefinitionProperty(
    new BooleanParameterDefinition('AUTOMATED_UPDATE', false, 'Set by the daily timer; manual runs require confirmation.')
))
job.addTrigger(new ExtendedTimerTrigger('''TZ=Pacific/Auckland
0 2 * * *
%AUTOMATED_UPDATE=true'''))
job.save()
JENKINS_UPDATE_PIPELINE_HOOK
chown jenkins:jenkins "$jenkins_home/init.groovy.d/97-homelab-server-update-pipeline.groovy"
chmod 0640 "$jenkins_home/init.groovy.d/97-homelab-server-update-pipeline.groovy"
install -o jenkins -g jenkins -m 0600 /dev/null "$jenkins_home/secrets/homelab-agent-enrollment.pending"

install -d -m 0755 /etc/systemd/system/jenkins.service.d
cat > /etc/systemd/system/jenkins.service.d/20-homelab.conf <<EOF
# Homelab controller: Java 25 and configured HTTP port.
[Service]
Environment="JENKINS_PORT=$JENKINS_PORT"
Environment="JENKINS_JAVA_CMD=$java25_path"
Environment="JAVA_HOME=${java25_path%/bin/java}"
EOF
systemctl daemon-reload
systemctl enable jenkins
systemctl restart jenkins
systemctl is-active --quiet jenkins || { systemctl --no-pager --full status jenkins; exit 1; }

secret_file="$jenkins_home/secrets/homelab-agent-bootstrap.secret"
for attempt in {1..120}; do
  [[ -s "$secret_file" ]] && break
  sleep 1
done
[[ -s "$secret_file" ]] || {
  echo "Jenkins started, but the automation node hook did not create its enrollment secret." >&2
  echo "Review /var/log/jenkins/jenkins.log for the Groovy hook error." >&2
  exit 1
}

healthy=no
for ((attempt=1; attempt<=60; attempt++)); do
  if curl --silent --show-error --max-time 5 --dump-header /tmp/homelab-jenkins-headers \
    --output /dev/null "http://127.0.0.1:$JENKINS_PORT/login" && \
    grep -qi '^X-Jenkins:' /tmp/homelab-jenkins-headers; then healthy=yes; break; fi
  sleep 5
done
rm -f /tmp/homelab-jenkins-headers
[[ $healthy == yes ]] || { printf 'Jenkins HTTP health check failed.\n' >&2; exit 1; }
date -Is > /var/lib/homelab/jenkins-controller-installed
printf 'Controller installed: Java 25, plugins, inbound node and homelab-check job.\n'
printf 'Complete the first administrator wizard; no passwords are printed.\n'
