# Starts Punctual and a public https link for phones. Run from PowerShell:
#   powershell -ExecutionPolicy Bypass -File .\start.ps1
# Passwords are typed when you run it and are never written to a file.
Set-Location $PSScriptRoot

# 1. MySQL has to be running (XAMPP: Control Panel -> MySQL -> Start)
function Test-Port($port) {  # quick check with a 2 s limit (Test-NetConnection can hang)
    try { $c = New-Object Net.Sockets.TcpClient; $r = $c.BeginConnect("127.0.0.1", $port, $null, $null); $ok = $r.AsyncWaitHandle.WaitOne(2000) -and $c.Connected; $c.Close(); return $ok } catch { return $false }
}
if (-not (Test-Port 3306)) {
    Write-Host "MySQL is not running. Start it, then run this again." -ForegroundColor Red
    exit 1
}

# 2. Passwords
function Ask($label) {
    $s = Read-Host $label -AsSecureString
    [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
}
$env:MYSQL_USER = "root"
$env:MYSQL_PASSWORD = Ask "MySQL password (press Enter if it has none)"
$env:ADMIN_PASSWORD = Ask "Admin password (8+ characters; only needed the very first time)"

# 3. Free port 3000 in case an old copy is still running
Get-NetTCPConnection -LocalPort 3000 -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }

# 4. The app, in its own window (it inherits the passwords above)
$app = Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$PSScriptRoot'; python main.py" -PassThru
Write-Host "Starting the app (the first run downloads the face models)..."
$up = $false
for ($i = 0; $i -lt 60 -and -not $up; $i++) {
    Start-Sleep 1
    $up = Test-Port 3000  # a plain port check: HTTP requests can be sent through a system proxy and fail
}
if (-not $up) { Write-Host "The app did not start. Look at the other window for the error." -ForegroundColor Red; exit 1 }
Write-Host "App is running:  http://localhost:3000   admin: http://localhost:3000/admin" -ForegroundColor Green

# 5. Cloudflare tunnel (http2 because many networks block the default QUIC)
$cf = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
if (-not $cf) { $cf = "C:\Program Files (x86)\cloudflared\cloudflared.exe" }
if (-not (Test-Path $cf)) { Write-Host "cloudflared is not installed. Run: winget install Cloudflare.cloudflared" -ForegroundColor Red; exit 1 }
$log = Join-Path $env:TEMP "punctual-tunnel.log"
Remove-Item $log -ErrorAction SilentlyContinue
$tunnel = Start-Process $cf -ArgumentList "tunnel", "--protocol", "http2", "--url", "http://localhost:3000" `
    -RedirectStandardError $log -WindowStyle Hidden -PassThru
$link = $null
for ($i = 0; $i -lt 40 -and -not $link; $i++) {
    Start-Sleep 1
    if (Test-Path $log) { $m = Select-String -Path $log -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" | Select-Object -First 1; if ($m) { $link = $m.Matches[0].Value } }
}
if ($link) {
    Set-Clipboard $link
    Write-Host ""
    Write-Host "Open this on your phone (copied to the clipboard):" -ForegroundColor Cyan
    Write-Host "  $link" -ForegroundColor Cyan
    Write-Host "It may take up to a minute before the link starts working."
} else {
    Write-Host "Could not read the tunnel link. Check $log" -ForegroundColor Yellow
}

# 6. Keep everything running until you say stop
Write-Host ""
Read-Host "Press Enter here to stop the app and the tunnel"
Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue
Get-NetTCPConnection -LocalPort 3000 -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
Stop-Process -Id $app.Id -Force -ErrorAction SilentlyContinue
Write-Host "Stopped."
