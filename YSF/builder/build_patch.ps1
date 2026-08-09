[CmdletBinding()]
param(
    [string]$Game2017,
    [string]$Game2020,
    [string]$OldPatch,
    [string]$Output,
    [string]$Python
)

$ErrorActionPreference = 'Stop'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $Utf8NoBom
[Console]::OutputEncoding = $Utf8NoBom
$OutputEncoding = $Utf8NoBom
$env:PYTHONUTF8 = '1'

function Normalize-UserPath {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $null
    }
    return $Value.Trim().Trim('"')
}

function Resolve-InputDirectory {
    param(
        [string]$Value,
        [string]$Prompt,
        [string[]]$RequiredFiles
    )

    $Value = Normalize-UserPath $Value
    if (-not $Value) {
        $Value = Normalize-UserPath (Read-Host $Prompt)
    }
    if (-not $Value) {
        throw "没有提供目录：$Prompt"
    }

    $Resolved = (Resolve-Path -LiteralPath $Value -ErrorAction Stop).Path
    $Item = Get-Item -LiteralPath $Resolved -Force -ErrorAction Stop
    if (-not $Item.PSIsContainer) {
        throw "路径不是目录：$Resolved"
    }
    foreach ($RelativePath in $RequiredFiles) {
        $RequiredPath = Join-Path $Resolved $RelativePath
        if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
            throw "目录内容不完整，缺少：$RequiredPath"
        }
    }
    return $Resolved
}

function Resolve-OutputDirectory {
    param([string]$Value)

    $Value = Normalize-UserPath $Value
    if (-not $Value) {
        $Value = Normalize-UserPath (Read-Host '输出目录（必须是尚不存在的新目录）')
    }
    if (-not $Value) {
        throw '没有提供输出目录。'
    }

    if ([IO.Path]::IsPathRooted($Value)) {
        $Resolved = [IO.Path]::GetFullPath($Value)
    }
    else {
        $Resolved = [IO.Path]::GetFullPath((Join-Path (Get-Location) $Value))
    }
    if (Test-Path -LiteralPath $Resolved) {
        throw "输出目录已经存在；为避免覆盖，请换一个新目录：$Resolved"
    }
    return $Resolved
}

$Game2017 = Resolve-InputDirectory $Game2017 `
    '2017 英文版游戏根目录' `
    @('ysf_win_dx9.exe', 'config_dx9.exe', 'release\data_us.ni', 'release\data_us.na')
$Game2020 = Resolve-InputDirectory $Game2020 `
    'XSEED 语音版游戏根目录' `
    @('ysf_win_dx9.exe', 'config_dx9.exe', 'release\data_us.ni', 'release\data_us.na')
$OldPatch = Resolve-InputDirectory $OldPatch `
    '原汉化补丁目录' `
    @(
        'ysf_win_cn_dx9.exe',
        'config_cn_dx9.exe',
        'ysfcn.dll',
        'ysfcn.text',
        'font.ttf',
        'release\data_cn.ni',
        'release\data_cn.na'
    )
$Output = Resolve-OutputDirectory $Output

$PythonCandidates = @()
if ($Python) {
    $Python = (Resolve-Path -LiteralPath (Normalize-UserPath $Python) -ErrorAction Stop).Path
    $PythonCandidates += [PSCustomObject]@{ Path = $Python; Prefix = @() }
}
else {
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($PythonCommand) {
        $PythonCandidates += [PSCustomObject]@{ Path = $PythonCommand.Source; Prefix = @() }
    }
    $PythonCommand = Get-Command py -ErrorAction SilentlyContinue
    if ($PythonCommand) {
        $PythonCandidates += [PSCustomObject]@{ Path = $PythonCommand.Source; Prefix = @('-3') }
    }
}

$Python = $null
$PythonPrefix = @()
foreach ($Candidate in $PythonCandidates) {
    $CandidatePrefix = @($Candidate.Prefix)
    & $Candidate.Path @CandidatePrefix -c `
        'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' `
        1>$null 2>$null
    if ($LASTEXITCODE -eq 0) {
        $Python = $Candidate.Path
        $PythonPrefix = $CandidatePrefix
        break
    }
}
if (-not $Python) {
    throw '没有找到可用的 Python 3.10 或更高版本。请安装新版 Python，或通过 -Python 指定 python.exe。'
}

Write-Host ''
Write-Host '即将使用以下路径：'
Write-Host "  2017 英文版：$Game2017"
Write-Host "  XSEED 语音版：$Game2020"
Write-Host "  原汉化补丁：$OldPatch"
Write-Host "  输出目录：$Output"
Write-Host "  Python：$Python"
Write-Host ''

& $Python @PythonPrefix (Join-Path $PSScriptRoot 'build_patch.py') `
    --game-2017 $Game2017 `
    --game-2020 $Game2020 `
    --old-patch $OldPatch `
    --output $Output

if ($LASTEXITCODE -ne 0) {
    throw "补丁构建失败，退出代码：$LASTEXITCODE"
}
