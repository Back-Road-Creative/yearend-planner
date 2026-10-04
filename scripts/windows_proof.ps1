# Phase 0 proof on a clean Windows machine. Runs the RELEASE ZIP, never the repo.
# Steps the plan requires: real calculation; path with a space and non-ASCII;
# moved folder; network blocked; standard (non-admin) user; cloud-sync refusal.
# -Inbox: a folder of synthetic PDFs (scripts/synthetic_inbox.py) run end to end.
param([Parameter(Mandatory = $true)][string]$Zip, [string]$Inbox = '')
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false   # PS 7.4+: a nonzero exit is ours to check
$expected = '$3,820.00'   # 2026 single, $50,000 wages: 10% x 12,400 + 12% x 21,500

$base = 'C:\pröof dir'
$dest = Join-Path $base 'planner ✓'
New-Item -ItemType Directory -Force -Path $dest | Out-Null
# Expand-Archive, Copy-Item and Compress-Archive took about half of a 29-minute
# proof on the bundle's tens of thousands of small files; .NET's zip and a
# multi-threaded robocopy do the same work in a fraction of the time.
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::ExtractToDirectory($Zip, $dest, $true)

function Copy-Tree([string]$From, [string]$To) {
    robocopy $From $To /E /MT:16 /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy $From -> $To exited $LASTEXITCODE" }
}

function Invoke-Planner([string]$Dir, [object]$Cmd, [int]$Expect = 0) {
    # PowerShell runs the launcher itself and takes the batch file's exit code;
    # going through `cmd.exe /c "..."` reported 0 for every exit (PR #1 proof).
    # $Cmd is a string split on spaces, or an array when an argument has one.
    $ErrorActionPreference = 'Continue'
    $argv = if ($Cmd -is [array]) { $Cmd } else { $Cmd.Split(' ') }
    $out = & "$Dir\planner.cmd" $argv 2>&1 | ForEach-Object { "$_" }
    $code = $LASTEXITCODE
    $out | ForEach-Object { Write-Host "  | $_" }
    if ($code -ne $Expect) { throw "planner.cmd $Cmd exited $code, expected $Expect" }
    return ($out -join "`n")
}

Write-Host "== 0. a failing command reports its exit code"
# Diagnostics first: every launcher style and the bare interpreter, so a wrong
# code is explained by this log rather than by another run.
$ErrorActionPreference = 'Continue'
& cmd.exe /c "`"$dest\planner.cmd`" no-such-command" *> $null
Write-Host "  cmd /c `"planner.cmd`"      -> $LASTEXITCODE"
& cmd.exe /c "call `"$dest\planner.cmd`" no-such-command" *> $null
Write-Host "  cmd /c call planner.cmd   -> $LASTEXITCODE"
& "$dest\planner.cmd" no-such-command *> $null
Write-Host "  & planner.cmd             -> $LASTEXITCODE"
$env:PLANNER_HOME = "$dest\"
& "$dest\python\python.exe" -m planner no-such-command *> $null
Write-Host "  python -m planner         -> $LASTEXITCODE"
Remove-Item Env:PLANNER_HOME
$ErrorActionPreference = 'Stop'
Invoke-Planner $dest 'no-such-command' 2 | Out-Null

Write-Host "== 1. version / paths / config in '$dest'"
Invoke-Planner $dest 'version' | Out-Null
Invoke-Planner $dest 'paths' | Out-Null
Invoke-Planner $dest 'check-config' | Out-Null
if (-not (Test-Path (Join-Path $dest 'data\inbox\UNMATCHED'))) { throw 'data layout missing' }

Write-Host '== 2. real calculation'
$o = Invoke-Planner $dest 'selfcheck'
if ($o -notlike "*$expected*") { throw "selfcheck did not print $expected" }

Write-Host '== 3. moved folder'
$moved = Join-Path $base 'moved après'
Move-Item -LiteralPath $dest -Destination $moved
$o = Invoke-Planner $moved 'selfcheck'
if ($o -notlike "*$expected*") { throw "selfcheck after move did not print $expected" }

Write-Host '== 4. network blocked'
$py = Join-Path $moved 'python\python.exe'
New-NetFirewallRule -DisplayName 'planner-proof-block' -Direction Outbound -Program $py -Action Block | Out-Null
try {
    $o = Invoke-Planner $moved 'selfcheck'
    if ($o -notlike "*$expected*") { throw 'selfcheck offline did not print the figure' }
    if ($Inbox) {
        # end to end on the unzipped release: synthetic documents in, page out
        $in = Join-Path $moved 'data\inbox'
        New-Item -ItemType Directory -Force -Path $in | Out-Null
        Copy-Item -Path (Join-Path $Inbox '*.pdf') -Destination $in
        foreach ($a in @(@('birth_date', '1971-06-15'), @('filing_status', 'single'),
                         @('state', 'NC'), @('wages', '52,000'))) {
            Invoke-Planner $moved @('enter', '--year', '2025', $a[0], $a[1]) | Out-Null
        }
        Invoke-Planner $moved 'run --quiet --no-update-check --year 2025 --as-of 2026-02-10' | Out-Null
        $page = Get-Content -LiteralPath (Join-Path $moved 'out\index.html') -Raw
        foreach ($shown in 'Example Bank (synthetic)', '1,235.00', '9,800.00') {
            if ($page -notlike "*$shown*") { throw "index.html after the offline run lacks $shown" }
        }
    }
} finally { Remove-NetFirewallRule -DisplayName 'planner-proof-block' }

Write-Host '== 5. standard user'
$user = 'plannerproof'
$pw = -join ((48..57 + 65..90 + 97..122) | Get-Random -Count 24 | ForEach-Object { [char]$_ })
$sec = ConvertTo-SecureString $pw -AsPlainText -Force
New-LocalUser -Name $user -Password $sec -PasswordNeverExpires | Out-Null
Add-LocalGroupMember -Group 'Users' -Member $user
icacls $base /grant "${user}:(OI)(CI)M" /T | Out-Null
$cred = New-Object System.Management.Automation.PSCredential($user, $sec)
$log = Join-Path $base 'standard-user.txt'
$p = Start-Process -FilePath 'cmd.exe' -ArgumentList "/c `"`"$moved\planner.cmd`" selfcheck`"" `
    -Credential $cred -LoadUserProfile -WorkingDirectory $moved -Wait -PassThru `
    -RedirectStandardOutput $log -RedirectStandardError "$log.err" -WindowStyle Hidden
Get-Content $log, "$log.err" -ErrorAction SilentlyContinue | ForEach-Object { "  | $_" }
if ($p.ExitCode -ne 0) { throw "standard user run exited $($p.ExitCode)" }
if ((Get-Content $log -Raw) -notlike "*$expected*") { throw 'standard user run did not print the figure' }
Remove-LocalUser -Name $user

Write-Host '== 6. cloud-sync folder refused'
$cloud = Join-Path $base 'OneDrive\planner'
New-Item -ItemType Directory -Force -Path $cloud | Out-Null
Copy-Tree $moved $cloud
# Steps 1-5 already created data/ and out/ in the source folder; the check below is
# that the refused run creates nothing, so start the copy without them.
Remove-Item -Recurse -Force -Path (Join-Path $cloud 'data'), (Join-Path $cloud 'out') -ErrorAction SilentlyContinue
$o = Invoke-Planner $cloud 'paths' 2
if ($o -notlike '*WARNING: this folder is inside a cloud-sync folder*') { throw 'planner.cmd did not warn' }
Invoke-Planner $cloud 'init' 2 | Out-Null
if (Test-Path (Join-Path $cloud 'data')) { throw 'data/ was created inside OneDrive' }

Write-Host '== 7. update, then roll back (planner.cmd moves the folders)'
$ver = (Get-Content -LiteralPath (Join-Path $moved 'VERSION')).Trim()
$next = "$ver.1"
$cand = Join-Path $base 'candidate'
New-Item -ItemType Directory -Force -Path $cand | Out-Null
foreach ($n in 'python', 'planner', 'config', 'templates') {
    Copy-Tree (Join-Path $moved $n) (Join-Path $cand $n)
}
foreach ($n in 'planner.cmd', 'LICENSE', 'README.md', 'GUIDE.md') {
    Copy-Item -LiteralPath (Join-Path $moved $n) -Destination $cand
}
Set-Content -LiteralPath (Join-Path $cand 'VERSION') -Value $next -Encoding ascii
$candZip = Join-Path $base "yearend-planner-$next-win64.zip"
[System.IO.Compression.ZipFile]::CreateFromDirectory($cand, $candZip)
$sha = (Get-FileHash -Algorithm SHA256 -LiteralPath $candZip).Hash.ToLower()
Set-Content -LiteralPath (Join-Path $moved 'data\private\keep.txt') -Value 'mine'
$o = Invoke-Planner $moved @('update', $candZip, '--sha256', $sha)
if ($o -notlike "*updated $ver -> $next*") { throw 'update did not report the new version' }
if ((Get-Content -LiteralPath (Join-Path $moved 'VERSION')).Trim() -ne $next) { throw 'VERSION not swapped' }
if (-not (Test-Path (Join-Path $moved 'data\engine-baseline.json'))) { throw 'update did not record the engine baseline' }
if ((Get-Content -LiteralPath (Join-Path $moved 'python-previous\VERSION')).Trim() -ne $ver) { throw 'previous not kept' }
$o = Invoke-Planner $moved 'version'
if ($o -notlike "*planner $next*") { throw 'version after update' }
$o = Invoke-Planner $moved 'update --rollback'
if ($o -notlike "*rolled back to $ver*") { throw 'rollback did not report' }
if ((Get-Content -LiteralPath (Join-Path $moved 'VERSION')).Trim() -ne $ver) { throw 'VERSION not restored' }
if (Test-Path (Join-Path $moved 'python-previous')) { throw 'python-previous left behind' }
if ((Get-Content -LiteralPath (Join-Path $moved 'data\private\keep.txt')) -ne 'mine') { throw 'data/ touched' }

Write-Host '== 8. the update feed: run stages it, the next run swaps it in and reruns'
Set-Content -LiteralPath "$candZip.sha256" -Value "$sha  yearend-planner-$next-win64.zip" -Encoding ascii
$feed = Join-Path $base 'latest.json'
$rel = @{ tag_name = "v$next"; assets = @(
    @{ name = "yearend-planner-$next-win64.zip"; browser_download_url = ([System.Uri]$candZip).AbsoluteUri },
    @{ name = "yearend-planner-$next-win64.zip.sha256"; browser_download_url = ([System.Uri]"$candZip.sha256").AbsoluteUri }) }
$rel | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $feed -Encoding utf8
$env:PLANNER_UPDATE_FEED = ([System.Uri]$feed).AbsoluteUri
try {
    $o = Invoke-Planner $moved 'run --quiet'
    if ($o -notlike "*update $next is ready*") { throw 'run did not stage the update' }
    $o = Invoke-Planner $moved 'run --quiet'
    if ($o -notlike "*updated $ver -> $next*") { throw 'run did not swap the update in' }
    if ($o -notlike '*written*index.html*') { throw 'run did not rerun on the new release' }
    if ((Get-Content -LiteralPath (Join-Path $moved 'VERSION')).Trim() -ne $next) { throw 'VERSION after feed update' }
} finally { Remove-Item Env:PLANNER_UPDATE_FEED }

Write-Host 'PROOF PASSED'
# The last launcher call above was the refused run (exit 2) and the Actions pwsh
# shell ends with 'exit $LASTEXITCODE'; a passed proof must say so itself.
exit 0
