param([string]$Python = 'python')

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$version = '1.6.1'
$sourceUrl = "https://downloads.xiph.org/releases/opus/opus-$version.tar.gz"
$sourceHash = '6ffcb593207be92584df15b32466ed64bbec99109f007c82205f0194572411a1'
$work = Join-Path $root 'logs/trace-opus'
$archive = Join-Path $work "opus-$version.tar.gz"
$source = Join-Path $work "opus-$version"
$build = Join-Path $work 'build'
$runtime = Join-Path $root 'src/trace_engine/assets/windows_x64'
$vswhere = 'C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe'
$installation = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
$installedVersion = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationVersion
$generator = switch -Regex ($installedVersion) {
    '^18\.' { 'Visual Studio 18 2026' }
    '^17\.' { 'Visual Studio 17 2022' }
    default { throw 'Install Visual Studio 2022 or 2026 with C++ and CMake tools' }
}
$cmake = Join-Path $installation 'Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin/cmake.exe'

New-Item -ItemType Directory -Force $work, $runtime | Out-Null
if (-not (Test-Path -LiteralPath $archive)) {
    curl.exe -fLsS --retry 2 --connect-timeout 10 --max-time 90 -o $archive $sourceUrl
    if ($LASTEXITCODE -ne 0) { throw 'Opus download failed' }
}
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLower() -ne $sourceHash) {
    throw 'Opus source archive checksum mismatch'
}
& $python -m tarfile --extract $archive $work --filter data
if ($LASTEXITCODE -ne 0) { throw 'Opus source extraction failed' }
& $cmake -S $source -B $build -G $generator -A x64 -DOPUS_BUILD_SHARED_LIBRARY=ON -DOPUS_STATIC_RUNTIME=ON -DOPUS_BUILD_TESTING=OFF -DOPUS_BUILD_PROGRAMS=OFF
if ($LASTEXITCODE -ne 0) { throw 'Opus configure failed' }
& $cmake --build $build --config Release --parallel
if ($LASTEXITCODE -ne 0) { throw 'Opus build failed' }

$dll = Join-Path $runtime 'opus.dll'
$license = Join-Path $runtime 'OPUS-LICENSE.txt'
Copy-Item -LiteralPath (Join-Path $build 'Release/opus.dll') -Destination $dll
Copy-Item -LiteralPath (Join-Path $source 'COPYING') -Destination $license
$manifest = [ordered]@{
    version = $version
    architecture = 'windows-x64'
    source_url = $sourceUrl
    source_sha256 = $sourceHash
    dll_sha256 = (Get-FileHash -LiteralPath $dll -Algorithm SHA256).Hash.ToLower()
    license_sha256 = (Get-FileHash -LiteralPath $license -Algorithm SHA256).Hash.ToLower()
} | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path $runtime 'opus-runtime.json'), $manifest + [Environment]::NewLine,
    [Text.UTF8Encoding]::new($false))
