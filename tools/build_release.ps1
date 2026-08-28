[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$')]
    [string] $Version,

    [string] $Ref = 'HEAD',

    [string] $OutputDirectory = 'dist'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$repositoryRoot = (& git -C $scriptDirectory rev-parse --show-toplevel).Trim()
if ($LASTEXITCODE -ne 0 -or -not $repositoryRoot) {
    throw 'Unable to locate the Git repository root.'
}

$resolvedCommit = (& git -C $repositoryRoot rev-parse "$Ref^{commit}").Trim()
if ($LASTEXITCODE -ne 0 -or -not $resolvedCommit) {
    throw "Ref does not resolve to a commit: $Ref"
}

if ([System.IO.Path]::IsPathRooted($OutputDirectory)) {
    $releaseDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
} else {
    $releaseDirectory = [System.IO.Path]::GetFullPath(
        (Join-Path $repositoryRoot $OutputDirectory)
    )
}

$packageName = "auto-paper-research-$Version"
$archivePath = Join-Path $releaseDirectory "$packageName.zip"
$checksumPath = "$archivePath.sha256"

New-Item -ItemType Directory -Path $releaseDirectory -Force | Out-Null
foreach ($target in @($archivePath, $checksumPath)) {
    if (Test-Path -LiteralPath $target) {
        throw "Refusing to overwrite an existing release artifact: $target"
    }
}

try {
    & git -C $repositoryRoot archive `
        --format=zip `
        "--prefix=$packageName/" `
        "--output=$archivePath" `
        $Ref
    if ($LASTEXITCODE -ne 0) {
        throw "git archive failed for ref: $Ref"
    }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($archivePath)
    try {
        $entryNames = @($archive.Entries | ForEach-Object { $_.FullName })
        $requiredEntries = @(
            "$packageName/README.md",
            "$packageName/.env.example",
            "$packageName/requirements-harness.txt",
            "$packageName/research_harness/__main__.py",
            "$packageName/skills/search-paper/SKILL.md",
            "$packageName/tools/wiki/__main__.py",
            "$packageName/wiki/_meta/schema.yaml",
            "$packageName/research/long-context-sparse-models/done-criteria.yaml",
            "$packageName/sources/papers/longlora-iclr-2024.pdf"
        )
        foreach ($requiredEntry in $requiredEntries) {
            if ($requiredEntry -notin $entryNames) {
                throw "Required release entry is missing: $requiredEntry"
            }
        }

        $forbiddenPatterns = @(
            '(^|/)(\.git|\.vscode|\.harness|dist|tmp|__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache)(/|$)',
            '(^|/)\.env$',
            '\.(db|sqlite|sqlite3)(-wal|-shm)?$',
            '\.py[co]$'
        )
        foreach ($entryName in $entryNames) {
            $relativeName = $entryName.Substring($packageName.Length).TrimStart('/')
            if (
                $relativeName.StartsWith('.env', [System.StringComparison]::OrdinalIgnoreCase) -and
                $relativeName -ne '.env.example'
            ) {
                throw "Forbidden credential file detected: $relativeName"
            }
            if (
                $relativeName.EndsWith('.pdf', [System.StringComparison]::OrdinalIgnoreCase) -and
                $relativeName -ne 'sources/papers/longlora-iclr-2024.pdf'
            ) {
                throw "Unexpected PDF detected in release archive: $relativeName"
            }
            foreach ($pattern in $forbiddenPatterns) {
                if ($relativeName -match $pattern) {
                    throw "Forbidden release entry detected: $relativeName"
                }
            }
        }
    } finally {
        $archive.Dispose()
    }

    $sha256 = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $checksumLine = "$sha256  $([System.IO.Path]::GetFileName($archivePath))"
    [System.IO.File]::WriteAllText(
        $checksumPath,
        $checksumLine + [Environment]::NewLine,
        [System.Text.UTF8Encoding]::new($false)
    )
} catch {
    foreach ($target in @($archivePath, $checksumPath)) {
        if (Test-Path -LiteralPath $target) {
            Remove-Item -LiteralPath $target -Force
        }
    }
    throw
}

[pscustomobject]@{
    Version = $Version
    Ref = $Ref
    Commit = $resolvedCommit
    Archive = $archivePath
    Checksum = $checksumPath
    Sha256 = $sha256
    Entries = $entryNames.Count
    Bytes = (Get-Item -LiteralPath $archivePath).Length
}
