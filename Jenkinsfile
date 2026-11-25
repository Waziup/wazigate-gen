pipeline {
  agent any
  options {
    // There was no timeout at all: a hung debootstrap ran until someone noticed.
    // Tighten this once stage0 is confirmed fast.
    timeout(time: 3, unit: 'HOURS')
    timestamps()
  }
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
        sh 'docker run --privileged --rm tonistiigi/binfmt --install arm64'
        sh 'sudo -E ./build.sh'
      }
    }
    stage('Deploy') {
      steps {
        dir('deploy') {
          sh "sudo zip image_nightly-WaziGate.zip nightly-WaziGate.img"
          catchError(buildResult: 'SUCCESS', stageResult: 'FAILURE') {
            // Flash to the SD card reader, addressed by its stable by-id path.
            // /dev/sda is assigned by enumeration order: if the reader is absent
            // or the bus order changes, that name can point at a system disk, and
            // this runs as root inside catchError. Skip rather than guess.
            sh '''
              CARD=/dev/disk/by-id/usb-Mass_Storage_Device_121220160204-0:0
              if ! sudo blockdev --getsize64 "$CARD" >/dev/null 2>&1; then
                echo "No readable SD card at $CARD -- skipping flash."
                exit 0
              fi
              echo "Flashing to $(readlink -f "$CARD")"
              sudo dd bs=4M if=nightly-WaziGate.img of="$CARD" conv=sync status=progress
              sync
            '''
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
