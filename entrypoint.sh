#!/bin/sh
set -e

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║       pdf-dispatch-tester  [DIAGNOSTIC]  ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Generate config.yaml ──────────────────────────────────────────────────────
cat > /app/config.yaml << YAML
server:    "${TESTER_SERVER:-http://localhost:5000}"
api_key:   "${TESTER_API_KEY:-pdf-dispatch-test-key}"
data_path: "${TESTER_DATA:-/data}"
webhook_host: "${WEBHOOK_HOST:-localhost}"
webhook_port: ${WEBHOOK_PORT:-5882}
smtp:
  host: ""
imap:
  host: ""
YAML

echo "=== NETWORK DIAGNOSTICS ==="
echo ""
echo "--- Hostname ---"
hostname
echo ""
echo "--- /etc/hosts ---"
cat /etc/hosts
echo ""
echo "--- Port status via Python ---"
python3 << 'PY'
import socket

tests = [
    ("localhost",    5000),
    ("127.0.0.1",   5000),
    ("localhost",   5881),
    ("127.0.0.1",  5881),
]

for host, port in tests:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2)
    r = s.connect_ex((host, port))
    s.close()
    status = "OPEN ✓" if r == 0 else f"closed (err {r})"
    print(f"  {host}:{port:5d}  →  {status}")

# Try resolving pdf-dispatch-test
try:
    ip = socket.gethostbyname("pdf-dispatch-test")
    print(f"  pdf-dispatch-test  →  resolves to {ip}")
    s = socket.socket()
    s.settimeout(2)
    r = s.connect_ex((ip, 5000))
    s.close()
    status = "OPEN ✓" if r == 0 else f"closed (err {r})"
    print(f"  {ip}:5000  →  {status}")
except Exception as e:
    print(f"  pdf-dispatch-test  →  DNS FAILED: {e}")
PY
echo ""
echo "--- Listening TCP ports (/proc/net/tcp) ---"
python3 << 'PY'
import socket, struct
with open('/proc/net/tcp') as f:
    lines = f.readlines()[1:]
for line in lines:
    parts = line.split()
    if parts[3] == '0A':  # state LISTEN
        port = int(parts[1].split(':')[1], 16)
        print(f"  listening on port {port}")
PY
echo ""
echo "==========================="
echo ""
echo "Install deps & exit (diagnostic run)"
pip install -r requirements.txt -q
echo "Done. Check diagnostics above."
