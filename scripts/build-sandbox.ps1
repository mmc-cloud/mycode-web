param(
    [Parameter(Mandatory = $true)]
    [string]$MyCodeSource,
    [string]$ImageTag = "mycode-sandbox:dev"
)

$ErrorActionPreference = "Stop"
$webRoot = Split-Path -Parent $PSScriptRoot
$sourceRoot = (Resolve-Path -LiteralPath $MyCodeSource).Path

foreach ($requiredPath in @("README.md", "pyproject.toml", "uv.lock", "mycode")) {
    if (-not (Test-Path -LiteralPath (Join-Path $sourceRoot $requiredPath))) {
        throw "MyCode source is missing required path: $requiredPath"
    }
}

docker buildx build `
    --load `
    --build-context "mycode=$sourceRoot" `
    --file (Join-Path $webRoot "docker/Dockerfile.sandbox") `
    --tag $ImageTag `
    $webRoot
