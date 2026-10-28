pipeline {
  agent any
  environment {
    IMG_NAME              = 'WaziGate'
    IMG_DATE              = 'nightly'
    ENABLE_SSH            = '1'
    FIRST_USER_PASS       = 'loragateway'
    TARGET_HOSTNAME       = 'wazigate'
    PI_GEN_REPO           = 'https://github.com/Waziup/WaziGate-ISO-gen'
    DEPLOY_ZIP            = '0'
    PUBKEY_SSH_FIRST_USER = 'ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQDIZzWQBgz9npw6at5EeVIBmtG3KsXRSfJogg7PECEIMQUaxgSVXeXEbSyzxZbzSqHWBoFJb1rJV7QyE4LAn7fOpFlpNDc+Fbh0SBGaV1IqKfRBJ09u2LQKhLUxMvy5MZuvmfjmufnv7GbWwjxt2BbUiXu4czQ1Y5kyt07otk7DTYXHflB08qtnR82mKtloODWdbkjenRmQaRNnuwy5ZfXb3PH3V6TdC1YzZhgaZyu6yPnSz68ks+RjA6ID67j1NS2NV+Sxnk5S1TqXP/PHsgQDUVUZNfm7xi/mhdltrwSNv57j2wrdNwXQGc05G2wJHU/+9SdBO1LObckw8WtRujznmU8MsylNLOJJVMOXxq65TNJYqfIv6TYPTV/7f6JBe2khLJ79zX6vv94b/i25Zxloc31mabT+/tqMttOpnjUYn6EGqAAbKiIWE0oJhlzcgPQKT1JkaqAzMU2UCLU1dkgJytYmzo55xek//SfMhH2C9/VPYqhBi7eS7KjWjTkMy+0= jenkins@cdupont-server'
    CLEAN                 = '1'
  }
  stages {
    stage('Build') {
      steps {
        sh 'sudo -E ./build.sh'
      }
    }
    stage('Deploy') {
      steps {
        dir('deploy') {
          sh "sudo zip image_nightly-WaziGate.zip nightly-WaziGate.img"
          catchError(buildResult: 'SUCCESS', stageResult: 'FAILURE') {
            // Flash to a SD card connected on /dev/sda
            sh "sudo dd bs=4M if=nightly-WaziGate.img of=/dev/sda conv=sync"
          }
        }
      }
    }
  }
  post {
    success {
      archiveArtifacts artifacts: 'deploy/*', fingerprint: true
    }
  }
}
