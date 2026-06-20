# pdf-dispatch-tester

Integration and end-to-end test suite for [pdf-dispatch](https://github.com/Lheriss/pdf-dispatch).

Tests run against a **real, deployed pdf-dispatch instance** — not mocks or stubs. They cover the REST API, PDF splitting behaviour (all trigger/placement combinations), outbound webhooks, email ingestion, and file-drop.

---

## Test phases

| Phase | File | What is tested | Server needed | Manual action |
|-------|------|---------------|--------------|---------------|
| 0 | `test_00_generator.py` | PDF generator self-tests | ❌ No | ❌ No |
| 1 | `test_01_api.py` | All REST endpoints | ✅ Yes | ❌ No |
| 2 | `test_02_processing.py` | Splitting behaviour (4 placement×page_handling cases, globs, case sensitivity…) | ✅ Yes | ❌ No |
| 3 | `test_03_webhook.py` | Webhook delivery, payload, HMAC | ✅ Yes | ❌ No |
| 4 | `test_04_email.py` | IMAP ingestion (SMTP send → poll → verify) | ✅ Yes | ⚠️ Test account |
| 5 | `test_05_filedrop.py` | Watched-folder ingestion | ✅ Yes | ⚠️ Drop file |

---

## Prerequisites

- Python 3.11+
- A running **test instance** of pdf-dispatch (see next section)
- For Phase 4: a dedicated test email account

---

## 1 — Set up a test instance of pdf-dispatch

> **Do not run tests against your production instance.**
> Tests reset statistics, change configuration, and write many output files.

### Option A — Docker Compose on the NAS (recommended)

Copy `docker-compose.test.yml` to your NAS and start the container:

```bash
# On the NAS (SSH)
mkdir -p /volume1/docker/pdf-dispatch-test
cd /volume1/docker/pdf-dispatch-test

# Set environment variables
export PUID=$(id -u)
export PGID=$(id -g)
export EMAIL_SECRET_TEST=$(openssl rand -hex 32)
export TEST_API_KEY="my-test-key-change-me"
export DATA_VOLUME_TEST=/volume1/docker/pdf-dispatch-test/data

docker compose -f /path/to/pdf-dispatch-tester/docker-compose.test.yml up -d
```

The test instance starts on **port 5881**.
Access it at `http://your-nas-ip:5881` to confirm it's running.

### Option B — Portainer stack

1. Create a new stack in Portainer named `pdf-dispatch-test`
2. Use `docker-compose.test.yml` as the compose file
3. Set environment variables in the Portainer stack settings:
   - `PUID` / `PGID` — your NAS user IDs (`id <youruser>` in SSH)
   - `EMAIL_SECRET_TEST` — `openssl rand -hex 32`
   - `TEST_API_KEY` — any string (e.g. `my-test-key`)
   - `DATA_VOLUME_TEST` — path to test data folder (e.g. `/volume1/docker/pdf-dispatch-test/data`)

### Option C — Local Docker (Mac)

```bash
DATA_VOLUME_TEST=/tmp/pdf-dispatch-test \
TEST_API_KEY=my-test-key \
EMAIL_SECRET_TEST=$(openssl rand -hex 32) \
PUID=1000 PGID=1000 \
docker compose -f docker-compose.test.yml up -d
```

The instance is then available at `http://localhost:5881`.

---

## 2 — (Phase 4 only) Email test account

Phase 4 tests email ingestion by sending a PDF as an attachment and
verifying that pdf-dispatch downloads and processes it.

**Recommended: dedicated Gmail account**

1. Create a new Gmail account (e.g. `pdf-dispatch-test@gmail.com`)
2. Enable 2-Step Verification on that account
3. Generate an App Password: [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
4. In the pdf-dispatch **test instance** web UI (port 5881):
   - Options → Configure email → Add configuration
   - Host: `imap.gmail.com`, Port: `993`, SSL: ✅
   - Username: `pdf-dispatch-test@gmail.com`, Password: the App Password
   - IMAP folder: `INBOX`, Poll interval: `1` minute
   - Action after download: **Mark as read** (keeps emails for inspection)
   - Save
5. Fill in the `smtp` and `imap` sections in `config.yaml` with the same credentials

---

## 3 — Installation

```bash
git clone https://github.com/Lheriss/pdf-dispatch-tester.git
cd pdf-dispatch-tester

python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

---

## 4 — Configuration

```bash
cp config.yaml.example config.yaml
```

Edit `config.yaml`:

```yaml
server:  "http://your-nas-ip:5881"    # URL of the test instance
api_key: "my-test-key"                # matches TEST_API_KEY above
```

`config.yaml` is in `.gitignore` and will never be committed.

---

## 5 — Running tests

```bash
# Phase 0 — generator self-tests (no server needed)
pytest tests/test_00_generator.py

# All automated phases (0–3)
pytest -m "not email and not filedrop"

# Full suite including email (Phase 4)
pytest -m "not filedrop"

# Full suite including semi-automated file-drop (Phase 5)
pytest

# Single phase
pytest tests/test_02_processing.py -v

# Open the HTML report after a run
open report/report.html     # macOS
xdg-open report/report.html # Linux
```

### Filtering by phase

```bash
pytest -m api          # REST API tests only
pytest -m processing   # splitting behaviour only
pytest -m webhook      # webhook tests only
pytest -m email        # email ingestion only (requires test account)
pytest -m filedrop     # semi-automated file-drop only
```

### Pointing at a different config

```bash
pytest --config /path/to/other-config.yaml
```

---

## 6 — Test structure

```
pdf-dispatch-tester/
├── config.yaml.example          ← copy to config.yaml, fill in values
├── docker-compose.test.yml      ← test instance of pdf-dispatch
├── pdf_generator.py             ← generates test PDFs with known barcode content
├── helpers.py                   ← shared utilities (upload_and_wait, set_config, …)
├── conftest.py                  ← pytest fixtures (server, http session, webhook receiver)
├── pytest.ini
├── requirements.txt
└── tests/
    ├── test_00_generator.py     ← Phase 0: PDF generator self-tests (no server)
    ├── test_01_api.py           ← Phase 1: REST API endpoints
    ├── test_02_processing.py    ← Phase 2: splitting behaviour
    ├── test_03_webhook.py       ← Phase 3: webhook delivery
    ├── test_04_email.py         ← Phase 4: IMAP ingestion
    └── test_05_filedrop.py      ← Phase 5: watched-folder (semi-auto)
```

### `pdf_generator.py` — how test PDFs are built

All test PDFs are generated on-the-fly using `reportlab` (PDF layout),
`segno` (QR codes), and `python-barcode` (Code128 barcodes).

Each page is described by a dict:

```python
from pdf_generator import make_pdf

pdf = make_pdf([
    {"kind": "content",  "text": "Document 1 — page 1"},
    {"kind": "qr",       "value": "FK3"},           # QR code centred on page
    {"kind": "content",  "text": "Document 2 — page 1"},
    {"kind": "code128",  "value": "INVOICE"},        # Code128 barcode
    {"kind": "content",  "text": "Document 3 — page 1"},
    {"kind": "multi",    "values": ["A", "B"]},      # two codes on one page
])
```

Pre-built fixtures (`fixture_one_trigger_before`, `fixture_two_triggers`, etc.)
cover the most common scenarios directly.

---

## 7 — What is tested (overview)

### Phase 0 — Generator
- Valid PDF output for all page kinds
- Corrupt PDF and non-PDF helpers
- Page count verification
- All pre-built fixtures

### Phase 1 — API (coming)
- `/healthz`, `/api/state`, `/api/config`
- Upload single / multiple files
- Task lifecycle (pending → success / error)
- File download
- Recent files
- API key authentication (missing / wrong)
- Statistics reset
- Webhook test endpoint

### Phase 2 — Processing (coming)
- `before + keep` / `before + delete` / `after + keep` / `after + delete`
- Single trigger, two triggers in sequence
- Three documents
- No trigger code → `no_code/`
- Corrupted PDF → `error/`
- Non-PDF file → `error/`
- Glob pattern matching (`FK*` matches `FK3`, `FK42`)
- Case-insensitive matching
- Two codes on the same page
- Per-file config override via upload

### Phase 3 — Webhook (coming)
- Delivery on successful split
- Delivery on error
- Payload structure and field values
- HMAC-SHA256 signature verification
- Events filter (`all` / `success` / `error`)
- `config_override` field in payload

### Phase 4 — Email ingestion (coming)
- PDF with trigger code → split and verified
- PDF without code → `no_code/`
- Sender filter (only processes matching sender)
- Subject filter
- Action after download (mark as read / delete / ignore)
- Default trigger for code-less attachments

### Phase 5 — File drop (coming, semi-automated)
- Interactive: test generates file, you drop it, test verifies
- Valid PDF with trigger
- Corrupted PDF
- Non-PDF file
- Multiple files in sequence
