// Homelab automation toolchain verification on the dedicated Jenkins agent.
pipeline {
    agent { label 'homelab-automation' }
    options {
        buildDiscarder(logRotator(numToKeepStr: '20'))
        timeout(time: 10, unit: 'MINUTES')
    }
    triggers { pollSCM('H/5 * * * *') }
    stages {
        stage('Verify automation worker') {
            steps {
                sh '''
                    set -eu
                    java -version
                    git --version
                    tofu version
                    packer version
                    task --version
                    ansible --version
                    ansible-config dump --only-changed | grep -F '/usr/local/share/ansible/collections'
                    ansible-galaxy collection list
                    /opt/ansible/bin/python -c 'import proxmoxer, requests'
                    for collection in community.proxmox community.routeros community.general kubernetes.core; do
                        ansible-galaxy collection list "$collection" | grep -F "$collection"
                    done
                '''
            }
        }
    }
}
