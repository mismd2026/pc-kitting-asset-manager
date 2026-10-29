<#
.SYNOPSIS
  Build a kitting USB: WinPE boot partition (FAT32) + data partition (NTFS) with
  Windows images and the generated pattern files.

.DESCRIPTION
  Requires the Windows ADK + WinPE add-on and an elevated PowerShell.
  1. kit-image build <pattern> --out dist   (on any machine, see README)
  2. .\New-KittingUsb.ps1 -DiskNumber 2 -Patterns standard,developer -ImageRoot D:\images

  FAT32 is required for UEFI boot but cannot hold files > 4 GB, so the boot files
  live on a small FAT32 partition and the WIMs on NTFS. WIMs larger than 4 GB are
  still split (.swm) when -SplitWim is given, for USBs that must stay FAT32-only.

.EXAMPLE
  .\New-KittingUsb.ps1 -DiskNumber 2 -Patterns standard,kiosk -ImageRoot D:\kitting
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'High')]
param(
  [Parameter(Mandatory)][int]$DiskNumber,
  [Parameter(Mandatory)][string[]]$Patterns,
  [Parameter(Mandatory)][string]$ImageRoot,          # contains images\ and drivers\
  [string]$DistDir = (Join-Path $PSScriptRoot '..\dist'),
  [string]$WinPEWork = "$env:TEMP\winpe_amd64",
  [switch]$SplitWim
)
$ErrorActionPreference = 'Stop'

$disk = Get-Disk -Number $DiskNumber
if ($disk.BusType -ne 'USB') { throw "Disk $DiskNumber is $($disk.BusType), not USB. Refusing to continue." }
if ($disk.Size -lt 16GB) { throw "USB must be at least 16 GB." }
foreach ($p in $Patterns) {
  if (-not (Test-Path (Join-Path $DistDir "$p\deploy.cmd"))) { throw "Pattern '$p' not built. Run: kit-image build $p" }
}
if (-not $PSCmdlet.ShouldProcess("Disk $DiskNumber ($($disk.FriendlyName), $([math]::Round($disk.Size/1GB)) GB)", 'ERASE and create kitting USB')) { return }

# 1. WinPE working files (ADK)
$adk = "${env:ProgramFiles(x86)}\Windows Kits\10\Assessment and Deployment Kit"
if (-not (Test-Path $WinPEWork)) {
  & cmd /c "`"$adk\Deployment Tools\DandISetEnv.bat`" && copype amd64 `"$WinPEWork`""
  if ($LASTEXITCODE) { throw 'copype failed (is the WinPE add-on installed?)' }
}

# 2. Partition: 2 GB FAT32 boot + rest NTFS data
$disk | Clear-Disk -RemoveData -RemoveOEM -Confirm:$false
Initialize-Disk -Number $DiskNumber -PartitionStyle MBR -ErrorAction SilentlyContinue
$boot = New-Partition -DiskNumber $DiskNumber -Size 2GB -IsActive -AssignDriveLetter
Format-Volume -Partition $boot -FileSystem FAT32 -NewFileSystemLabel 'WINPE' -Confirm:$false | Out-Null
$data = New-Partition -DiskNumber $DiskNumber -UseMaximumSize -AssignDriveLetter
Format-Volume -Partition $data -FileSystem NTFS -NewFileSystemLabel 'KITTING' -Confirm:$false | Out-Null
$B = "$($boot.DriveLetter):"; $D = "$($data.DriveLetter):"

# 3. WinPE media + auto-start menu
robocopy "$WinPEWork\media" "$B\" /E /NFL /NDL /NJH /NJS | Out-Null
$mount = "$WinPEWork\mount"
New-Item -ItemType Directory -Force $mount | Out-Null
Mount-WindowsImage -ImagePath "$B\sources\boot.wim" -Index 1 -Path $mount | Out-Null
try {
  $menu = @('@echo off', 'wpeinit', 'for %%d in (C D E F G H I J K L M N O P) do if exist %%d:\kitting\menu.cmd set KITUSB=%%d:', 'call %KITUSB%\kitting\menu.cmd')
  Set-Content -Path "$mount\Windows\System32\startnet.cmd" -Value $menu -Encoding ASCII
} finally {
  Dismount-WindowsImage -Path $mount -Save | Out-Null
}

# 4. Images, drivers and pattern files on the data partition
New-Item -ItemType Directory -Force "$D\kitting" | Out-Null
$lines = @('@echo off', 'echo.', 'echo  Kitting patterns:')
$i = 0
foreach ($p in $Patterns) {
  $i++
  Copy-Item -Recurse -Force (Join-Path $DistDir $p) "$D\kitting\$p"
  $m = Get-Content (Join-Path $DistDir "$p\manifest.json") -Raw | ConvertFrom-Json
  $lines += "echo   $i. $p"
  foreach ($rel in @($m.image.wim, $m.image.drivers) | Where-Object { $_ }) {
    $src = Join-Path $ImageRoot $rel; $dst = Join-Path $D $rel
    if (Test-Path "$dst*") { continue }
    New-Item -ItemType Directory -Force (Split-Path $dst) | Out-Null
    if ($SplitWim -and $rel -like '*.wim' -and (Get-Item $src).Length -gt 4GB) {
      Split-WindowsImage -ImagePath $src -SplitImagePath ($dst -replace '\.wim$', '.swm') -FileSize 3800 -CheckIntegrity | Out-Null
    } else {
      robocopy (Split-Path $src) (Split-Path $dst) (Split-Path $src -Leaf) /E /NFL /NDL /NJH /NJS | Out-Null
    }
  }
}
$lines += 'set /p N=Select pattern number: '
$i = 0
foreach ($p in $Patterns) { $i++; $lines += "if `"%N%`"==`"$i`" call %~d0\kitting\$p\deploy.cmd" }
Set-Content -Path "$D\kitting\menu.cmd" -Value $lines -Encoding ASCII

Write-Host "Kitting USB ready: boot=$B data=$D patterns=$($Patterns -join ', ')" -ForegroundColor Green
Write-Warning 'The USB contains the enroll token and a temporary admin password. Store it like a key.'
