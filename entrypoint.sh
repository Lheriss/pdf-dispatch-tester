#!/bin/sh
set -e

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║       pdf-dispatch-tester                ║"
echo "╚══════════════════════════════════════════╝"
echo ""

cat > /app/config.yaml << YAML
server:    "${TESTER_SERVER:-http://localhost:5000}"
api_key:   "${TESTER_API_KEY:-pdf-dispatch-test-key}"
data_path: "${TESTER_DATA:-/data}"
webhook_host: "${WEBHOOK_HOST:-localhost}"
webhook_port: ${WEBHOOK_PORT:-5882}
smtp:
  host:     "${SMTP_HOST:-}"
  port:     ${SMTP_PORT:-587}
  user:     "${SMTP_USER:-}"
  password: "${SMTP_PASSWORD:-}"
imap:
  host:     "${IMAP_HOST:-}"
  port:     ${IMAP_PORT:-993}
  user:     "${IMAP_USER:-}"
  password: "${IMAP_PASSWORD:-}"
  folder:   "${IMAP_FOLDER:-INBOX}"
YAML

echo "  server:       ${TESTER_SERVER:-http://localhost:5000}"
echo "  webhook_host: ${WEBHOOK_HOST:-localhost}:${WEBHOOK_PORT:-5882}"
echo "  data:         ${TESTER_DATA:-/data}"
echo ""

echo "Installing Python dependencies..."
pip install -r requirements.txt -q
echo ""

SERVER="${TESTER_SERVER:-http://localhost:5000}"
echo "Waiting for pdf-dispatch at ${SERVER}..."
python3 -c "
import urllib.request, sys, time
for i in range(30):
    try:
        urllib.request.urlopen('${SERVER}/healthz', timeout=2)
        print('  OK')
        sys.exit(0)
    except:
        print('.', end='', flush=True)
        time.sleep(1)
print()
print('ERROR: pdf-dispatch not reachable after 30s')
sys.exit(1)
"

echo ""

# ── Mode switch ───────────────────────────────────────────────────────────────
# WEB_MODE=1  → start the web interface on WEB_PORT (default 5883)
# WEB_MODE=0  → run pytest once with PYTEST_ARGS and exit  (CI / automated)
if [ "${WEB_MODE:-1}" = "1" ]; then
    echo "Starting web interface on port ${WEB_PORT:-5883}..."
    echo "Open: http://<NAS_IP>:${WEB_PORT:-5883}"
    echo ""
    exec python /app/web_runner.py
else
    TS=$(date +%Y-%m-%d_%H-%M-%S)
    ARGS="${PYTEST_ARGS:--v --tb=short}"
    REPORT="report/report_${TS}.html"
    echo "Running: pytest ${ARGS}"
    echo "Report:  ${REPORT}"
    echo ""
    exec python -m pytest $ARGS --html="$REPORT" --self-contained-html
fi
