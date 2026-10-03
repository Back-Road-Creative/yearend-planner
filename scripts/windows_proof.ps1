# Phase 0 proof on a clean Windows machine. Runs the RELEASE ZIP, never the repo.
# Steps the plan requires: real calculation; path with a space and non-ASCII;
# moved folder; network blocked; standard (non-admin) user; cloud-sync refusal.
param([Parameter(Mandatory = $true)][string]$Zip)
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false   # PS 7.4+: a nonzero exit is ours to check
$expected = '$3,820.00'   # 2026 single, $50,000 wages: 10% x 12,400 + 12% x 21,500

$base = 'C:\pröof dir'
$dest = Join-Path $base 'planner ✓'
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Expand-Archive -LiteralPath $Zip -DestinationPath $dest -Force

function Invoke-Planner([string]$Dir, [string]$Cmd, [int]$Expect = 0) {
    # PowerShell runs the launcher itself and takes the batch file's exit code;
    # going through `cmd.exe /c "..."` reported 0 for every exit (PR #1 proof).
    $ErrorActionPreference = 'Continue'
    $out = & "$Dir\planner.cmd" $Cmd.Split(' ') 2>&1 | ForEach-Object { "$_" }
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
Copy-Item -Path (Join-Path $moved '*') -Destination $cloud -Recurse
# Steps 1-5 already created data/ and out/ in the source folder; the check below is
# that the refused run creates nothing, so start the copy without them.
Remove-Item -Recurse -Force -Path (Join-Path $cloud 'data'), (Join-Path $cloud 'out') -ErrorAction SilentlyContinue
Invoke-Planner $cloud 'paths' 2 | Out-Null
if (Test-Path (Join-Path $cloud 'data')) { throw 'data/ was created inside OneDrive' }

Write-Host 'PROOF PASSED'
