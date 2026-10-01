# Downloads a portable JDK + Android SDK into <repo>\android-toolchain (no system install).
# Safe to re-run: each step is skipped if already present.
# Linux/macOS users should run scripts/build_apk.py instead.
$ErrorActionPreference = 'Stop'
$root = Join-Path (Split-Path $PSScriptRoot -Parent) 'android-toolchain'
New-Item -ItemType Directory -Force -Path $root | Out-Null
$log = Join-Path $root 'install.log'
function Say($m) { "$(Get-Date -Format HH:mm:ss)  $m" | Tee-Object -FilePath $log -Append }

# --- 1. JDK 17 (Temurin, Windows x64, zip) --------------------------------
$jdk = Join-Path $root 'jdk'
if (Test-Path (Join-Path $jdk 'bin\java.exe')) {
    Say 'JDK already present'
} else {
    Say 'Resolving JDK 17 URL...'
    $rel = Invoke-RestMethod -Uri 'https://api.adoptium.net/v3/assets/feature_releases/17/ga?architecture=x64&image_type=jdk&os=windows&vendor=eclipse&page_size=1'
    $url = $rel[0].binaries[0].package.link
    $zip = Join-Path $root 'jdk.zip'
    Say "Downloading JDK ($([math]::Round($rel[0].binaries[0].package.size/1MB,1)) MB)..."
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    Say 'Extracting JDK...'
    Expand-Archive -Path $zip -DestinationPath (Join-Path $root 'jdkraw') -Force
    $inner = Get-ChildItem (Join-Path $root 'jdkraw') -Directory | Select-Object -First 1
    Move-Item $inner.FullName $jdk
    Remove-Item (Join-Path $root 'jdkraw') -Recurse -Force
    Remove-Item $zip -Force
}
Say ('java: ' + (& (Join-Path $jdk 'bin\java.exe') -version 2>&1 | Select-Object -First 1))

# --- 2. Android command-line tools ---------------------------------------
$cmdline = Join-Path $root 'cmdline-tools'
if (Test-Path (Join-Path $cmdline 'latest\bin\sdkmanager.bat')) {
    Say 'cmdline-tools already present'
} else {
    $zip = Join-Path $root 'cmdline.zip'
    Say 'Downloading Android command-line tools...'
    Invoke-WebRequest -Uri 'https://dl.google.com/android/repository/commandlinetools-win-11076708_latest.zip' -OutFile $zip -UseBasicParsing
    Say 'Extracting...'
    Expand-Archive -Path $zip -DestinationPath (Join-Path $root 'cmdraw') -Force
    New-Item -ItemType Directory -Force -Path $cmdline | Out-Null
    Move-Item (Join-Path $root 'cmdraw\cmdline-tools') (Join-Path $cmdline 'latest')
    Remove-Item (Join-Path $root 'cmdraw') -Recurse -Force
    Remove-Item $zip -Force
}

# --- 3. Platform + build-tools -------------------------------------------
$sdk = Join-Path $root 'sdk'
$env:JAVA_HOME = $jdk
$env:ANDROID_SDK_ROOT = $sdk
$sdkm = Join-Path $cmdline 'latest\bin\sdkmanager.bat'
Say 'Accepting licences...'
('' | & $sdkm --sdk_root="$sdk" --licenses) 2>&1 | Out-Null
Say 'Installing platform-tools, platforms;android-34, build-tools;34.0.0...'
& $sdkm --sdk_root="$sdk" 'platform-tools' 'platforms;android-34' 'build-tools;34.0.0' 2>&1 |
    ForEach-Object { $_ | Out-File -FilePath $log -Append }
Say 'DONE'
