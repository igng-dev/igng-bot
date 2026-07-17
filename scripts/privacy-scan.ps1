[CmdletBinding()]
param(
    [switch]$Staged
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$excluded = @(
    ".git/", ".venv/", "runtime/", "__pycache__/", ".agents/", ".claude/",
    ".codex/", ".gemini/", "_upstream_astrbot/", "老程序（仅留档）/", "老程序v2（仅供了解功能）/",
    "scripts/privacy-scan.ps1"
)
$patterns = @(
    '(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----',
    '(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password|passwd|secret|websocket[_-]?key)\s*[:=]\s*["''][^"'']{8,}["'']',
    '(?i)\b(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{20,})\b',
    '(?i)\b(mysql|postgres|mongodb(?:\+srv)?)://[^\s"'']+:[^\s"'']+@',
    '(?i)\b(ssh-rsa|ssh-ed25519)\s+[A-Za-z0-9+/]{80,}={0,2}'
)

if ($Staged) {
    $paths = @(git diff --cached --name-only --diff-filter=ACMR)
    $content = @(git diff --cached --binary -- .)
    $label = "staged changes"
} else {
    $paths = @(git ls-files --cached --others --exclude-standard)
    $content = @()
    foreach ($path in $paths) {
        $normalized = $path.Replace('\', '/')
        if ($excluded | Where-Object { $normalized.StartsWith($_) }) { continue }
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            $content += Get-Content -LiteralPath $path -Raw -ErrorAction SilentlyContinue
        }
    }
    $label = "working tree"
}

$findings = @()
if ($Staged) {
    $content = @($content -join "`n")
}
foreach ($pattern in $patterns) {
    $matches = [regex]::Matches(($content -join "`n"), $pattern)
    foreach ($match in $matches) {
        $findings += $match.Value.Substring(0, [Math]::Min(120, $match.Value.Length))
    }
}

if ($findings.Count -gt 0) {
    Write-Error "Privacy scan failed for $label. Remove or replace these suspected secrets: $($findings -join '; ')"
    exit 1
}
Write-Output "Privacy scan passed: $label ($($paths.Count) paths checked)."
