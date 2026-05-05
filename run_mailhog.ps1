# Run MailHog using Docker. Exposes SMTP on 1025 and web UI on 8025.
# Usage: .\run_mailhog.ps1

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "Docker not found. Please install Docker Desktop or run MailHog binary manually." -ForegroundColor Yellow
    exit 1
}

Write-Host "Starting MailHog container (mailhog/mailhog) ..."

docker run --rm -p 1025:1025 -p 8025:8025 --name mailhog mailhog/mailhog
