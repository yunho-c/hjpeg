[CmdletBinding()]
param(
    [switch]$RebuildImage,
    [string]$Image = "hjpeg-test:local",
    [ValidateRange(1, 64)]
    [int]$SbtCpus = 4,
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$SbtArguments
)

$ErrorActionPreference = "Stop"

if ($null -eq (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker Desktop is required. Install and start Docker Desktop, then rerun this script."
}

& docker info 1>$null 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Desktop is installed but its engine is not running. Start Docker Desktop, wait until it reports 'Engine running', then rerun this script."
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$dockerfile = Join-Path $PSScriptRoot "Dockerfile"

$imageIds = @(& docker image ls --quiet --no-trunc $Image)
if ($LASTEXITCODE -ne 0) {
    throw "Could not query Docker image '$Image'."
}
$imageExists = $imageIds.Count -gt 0 -and -not [string]::IsNullOrWhiteSpace($imageIds[0])
if ($RebuildImage -or -not $imageExists) {
    & docker build --tag $Image --file $dockerfile $repoRoot
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

if ($null -eq $SbtArguments -or $SbtArguments.Count -eq 0) {
    $SbtArguments = @("test")
}

$runArguments = @(
    "run",
    "--rm",
    "--init",
    "--mount", "type=bind,source=$repoRoot,target=/workspace",
    "--mount", "type=volume,source=hjpeg-sbt-cache,target=/cache",
    "--env", "HOME=/cache/home",
    "--env", "COURSIER_CACHE=/cache/coursier",
    "--env", "SBT_OPTS=-Dsbt.global.base=/cache/sbt -Dsbt.ivy.home=/cache/ivy -XX:ActiveProcessorCount=$SbtCpus",
    $Image
)

foreach ($variable in @("HJPEG_PERFORMANCE_CAPTURE_DIR", "HJPEG_PERFORMANCE_SCENARIOS")) {
    $value = [Environment]::GetEnvironmentVariable($variable)
    if (-not [string]::IsNullOrEmpty($value)) {
        $runArguments = $runArguments[0..($runArguments.Count - 2)] + @("--env", "$variable=$value") + $runArguments[($runArguments.Count - 1)]
    }
}

$runArguments += $SbtArguments

& docker @runArguments
exit $LASTEXITCODE
