@echo off
cd /d "%~dp0"
git commit --allow-empty -m "chore: keepalive to wake GitHub Actions schedule"
git push origin main
echo.
echo Done! Check Actions at https://github.com/ivanhtlin/ticket-master/actions
pause
