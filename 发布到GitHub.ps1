# ─────────────────────────────────────────────────────────────
#  发布到 GitHub —— 一键脚本
# ─────────────────────────────────────────────────────────────
#  用法：先完成 gh 登录，然后双击「发布到GitHub.cmd」
#
#     或手动：gh auth login
#             （GitHub.com → HTTPS → Login with a web browser）
#
#  本脚本会：读取你的 GitHub 用户名 → 回填 package.json 仓库地址
#            → 提交 → 创建公开仓库 → 推送
# ─────────────────────────────────────────────────────────────

$ErrorActionPreference = 'Stop'
$RepoName = 'quant-spider'          # 想换名字就改这里

# gh 装完后当前终端可能还没刷新 PATH，这里直接找绝对路径
$gh = @(
    "C:\Program Files\GitHub CLI\gh.exe",
    "$env:LOCALAPPDATA\Programs\GitHub CLI\gh.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $gh) { $gh = (Get-Command gh -ErrorAction SilentlyContinue).Source }
if (-not $gh) {
    Write-Host "找不到 gh.exe。请先安装：winget install --id GitHub.cli" -ForegroundColor Red
    exit 1
}

Write-Host "=== 1. 检查登录状态 ===" -ForegroundColor Cyan
$login = & $gh api user --jq .login 2>$null
if (-not $login) {
    Write-Host "尚未登录，请先运行：" -ForegroundColor Yellow
    Write-Host "    `"$gh`" auth login" -ForegroundColor White
    Write-Host "  然后选 GitHub.com → HTTPS → Login with a web browser"
    exit 1
}
Write-Host "  已登录为 $login" -ForegroundColor Green

Write-Host ""
Write-Host "=== 2. 回填 package.json 的仓库地址 ===" -ForegroundColor Cyan
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pkg = Join-Path $root 'package.json'
$c = Get-Content $pkg -Raw -Encoding utf8
$c = $c -replace 'github\.com/OWNER/', "github.com/$login/"
[System.IO.File]::WriteAllText($pkg, $c, (New-Object System.Text.UTF8Encoding $false))
Select-String -Path $pkg -Pattern 'github\.com' | ForEach-Object { Write-Host "  $($_.Line.Trim())" }

Push-Location $root

Write-Host ""
Write-Host "=== 3. 提交改动 ===" -ForegroundColor Cyan
& git add -A
if (& git status --porcelain) {
    & git -c core.quotepath=false commit -q -m "chore: 回填仓库地址为 $login/$RepoName"
    Write-Host "  已提交" -ForegroundColor Green
} else {
    Write-Host "  无改动需要提交"
}

Write-Host ""
Write-Host "=== 4. 创建公开仓库并推送 ===" -ForegroundColor Cyan
$already = & $gh repo view "$login/$RepoName" --json name 2>$null
if ($already) {
    Write-Host "  仓库已存在，改为直接推送" -ForegroundColor Yellow
    if (-not (& git remote get-url origin 2>$null)) {
        & git remote add origin "https://github.com/$login/$RepoName.git"
    } else {
        & git remote set-url origin "https://github.com/$login/$RepoName.git"
    }
    & git push -u origin main
} else {
    & $gh repo create $RepoName --public --source=. --remote=origin --push `
        --description "真实行情量化看板（数据源/指标/策略/回测/模拟盘/自动交易）+ Canvas 2D 蜘蛛过程可视化"
}

Write-Host ""
if ($LASTEXITCODE -eq 0) {
    Write-Host "=== 完成 ===" -ForegroundColor Green
    Write-Host "  https://github.com/$login/$RepoName" -ForegroundColor White
} else {
    Write-Host "推送失败，请把上面的报错发给我" -ForegroundColor Red
}
Pop-Location
