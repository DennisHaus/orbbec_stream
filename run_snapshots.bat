@echo off
rem Fill these in from Cloudflare: R2 - Manage API tokens - Object Read and Write, this bucket only
set R2_ACCOUNT_ID=your-account-id
set R2_ACCESS_KEY_ID=your-access-key-id
set R2_SECRET_ACCESS_KEY=your-secret-access-key
set R2_BUCKET=astra-snapshots

cd /d "%~dp0"
rem One snapshot every 15 minutes, on the quarter hour
python snapshotter.py --interval 15
pause
