# pdf-dispatch-tester

Suite de tests d'intégration et end-to-end pour [pdf-dispatch](https://github.com/Lheriss/pdf-dispatch).

Les tests s'exécutent contre une **instance réelle et déployée** de pdf-dispatch, partagent le même volume `/data`, et couvrent le moteur de splitting, l'API REST, les webhooks, l'ingestion IMAP et l'interface web (Playwright/Chromium).

---

## Phases

| Phase | Fichier | Ce qui est testé | Serveur | Filesystem | Greenmail | Browser |
|-------|---------|-----------------|---------|-----------|-----------|---------|
| 0 | `test_00_generator.py` | Générateur de PDFs (auto-tests) | ❌ | ❌ | ❌ | ❌ |
| 1 | `test_01_processing.py` | Moteur de splitting — placement × page_handling, glob, casse, adversarial, cas limites | ✅ | ✅ | ❌ | ❌ |
| 2 | `test_02_api.py` | Endpoints REST (upload, tâches, config, sécurité) | ✅ | ❌ | ❌ | ❌ |
| 3 | `test_03_webhook.py` | Webhook sortant, payload, HMAC | ✅ | ❌ | ❌ | ❌ |
| 4 | `test_04_email.py` | Ingestion IMAP — pipeline SMTP → Greenmail → pdf-dispatch → /data | ✅ | ✅ | ✅ | ❌ |
| 6 | `test_06_security.py` | Sécurité API — injection config, traversal, clés | ✅ | ❌ | ❌ | ❌ |
| 7 | `test_08_input_validation.py` | Validation entrées — CRLF, bornes port, log injection | ✅ | ❌ | ❌ | ❌ |
| 9 | `test_09_ui.py` | Interface web — Playwright/Chromium headless | ✅ | ❌ | ❌ | ✅ |

---

## Architecture

Un seul stack Portainer (`pdf-dispatch-test`) contenant trois services :

```
Stack "pdf-dispatch-test"  (docker-compose.test.yml)
│
├── pdf-dispatch-test    ghcr.io/lheriss/pdf-dispatch:latest
│   ├── port 5881         ← UI pdf-dispatch
│   └── /data ──────────────────────────────────┐
│                                                │ volume partagé hôte
├── pdf-dispatch-tester  ghcr.io/lheriss/pdf-dispatch-tester:latest
│   ├── port 5883         ← UI tester (WEB_MODE=1)
│   └── /data ──────────────────────────────────┘
│
└── greenmail            greenmail/standalone:2.0.1
    ├── SMTP :3025 / IMAP :3143 (plain)
    └── IMAPS :3993 / SMTPS :3465
```

Les deux containers partagent le même dossier `/data` sur l'hôte.
Le tester écrit des PDFs dans `/data/input/`, pdf-dispatch les traite, le tester lit `/data/output/` directement.

Les tests email utilisent Greenmail (serveur IMAP/SMTP embarqué dans le stack, aucune configuration externe requise).

---

## Déploiement dans Portainer

### Stack unique — `pdf-dispatch-test`

**Portainer → Stacks → Add stack → Repository**

| Champ | Valeur |
|-------|--------|
| Name | `pdf-dispatch-test` |
| Repository URL | `https://github.com/Lheriss/pdf-dispatch-tester` |
| Compose path | `docker-compose.test.yml` |
| Authentication | Token GitHub (scopes `repo` + `read:packages`) |
| GitOps updates | ✅ activé |

#### Variables d'environnement à configurer

| Variable | Exemple | Description |
|----------|---------|-------------|
| `PUID` | `1026` | `id -u <tonuser>` en SSH |
| `PGID` | `100` | `id -g <tonuser>` en SSH |
| `TZ` | `Europe/Zurich` | Fuseau horaire |

Les autres variables (clés API, chemins, ports) sont déjà définies dans `docker-compose.test.yml` avec des valeurs de test isolées.

→ **Deploy the stack**

Vérifier :
- `http://ton-nas:5881` → interface pdf-dispatch sous test
- `http://ton-nas:5883` → interface web du tester

---

## Utiliser l'interface web (port 5883)

Le tester démarre en `WEB_MODE=1` par défaut : une interface Flask reste active en continu. Vous pouvez y déclencher n'importe quelle phase ou sous-groupe de tests d'un clic.

- **Phase 9 (Playwright UI)** nécessite que Chromium soit installé dans le container.  
  Ce n'est le cas qu'après un **rebuild de l'image tester** — opération requise si le `Dockerfile` a changé.

### Quand rebuild vs simple re-pull

| Changement dans le repo tester | Action Portainer |
|-------------------------------|-----------------|
| Code Python (`.py`), compose, fixtures | Re-pull image → Recreate container |
| `Dockerfile` (ex: ajout Playwright) | **Delete + Add stack** (rebuild de l'image CI) |
| Variables d'environnement ou structure compose | **Delete + Add stack** |

---

## Consulter les résultats

**En direct :** onglet **Results** de l'interface web tester (port 5883)

**Logs persistants sur le NAS** (`/volume1/docker/pdf_test/logs/<timestamp>/`) :

| Fichier | Contenu |
|---------|---------|
| `session.log` | Log chronologique lisible (PASS/FAIL, timings, corps des requêtes) |
| `http_traffic.jsonl` | Chaque appel HTTP en JSON complet |
| `pdfdispatch.log` | Journal pdf-dispatch capturé aux points clés |

**Rapport HTML :** `/volume1/docker/pdf_test/report/report_<timestamp>.html`

---

## Notes par phase

### Phase 1 — Moteur de splitting

Les fixtures adversariales (PDFs tronqués, non-PDF avec extension `.pdf`, barcodes dégradés) entraînent une consommation mémoire élevée lors du scan ZXING à 300 DPI. En cas d'exit code 137 (OOM), réduire `BARCODE_DPI` à 150 dans `docker-compose.test.yml`.

### Phase 4 — Email IMAP

Greenmail est démarré automatiquement dans le stack. Aucun compte email externe n'est nécessaire. Le tester envoie des emails via SMTP vers Greenmail, pdf-dispatch les poll via IMAP, le résultat apparaît dans `/data/output/`.

> Le premier scan de barcode dans une session fraîche peut prendre 60–90 s (démarrage à froid de la JVM ZXING). Les tests email les plus lents ont un timeout de 150 s pour absorber ce cas.

### Phase 9 — UI Playwright

Nécessite Chromium headless dans le container (installé via `playwright install --with-deps chromium` dans le `Dockerfile`). Le container tester a `shm_size: 512m` pour éviter les crashs Chromium.

**Sous-groupes disponibles dans l'UI :**
- `phase9_smoke` — 7 tests · page charge, JS sain, stats numériques, i18n appliquée
- `phase9c` — 9 tests · triggers CRUD (add, config, delete, persistence)
- `phase9e` — 4 tests · options (séparateur, toggles, persistence)
- `phase9f` — 8 tests · panneau email (radios, SSL toggle Safari, dropdown)

---

## Structure du projet

```
pdf-dispatch-tester/
├── Dockerfile                        ← python:3.12-slim + playwright chromium
├── docker-compose.test.yml           ← stack complet (pdf-dispatch + tester + greenmail)
├── entrypoint.sh                     ← génère config.yaml depuis env vars, démarre Flask/pytest
├── web_runner.py                     ← interface web Flask (WEB_MODE=1)
│
├── pdf_generator.py                  ← fabrique de PDFs de test (QR, Code128, adversarial)
├── file_dropper.py                   ← écrit /data/input/, lit /data/output/ (pypdf)
├── helpers.py                        ← upload_and_wait, set_config, set_triggers, snapshot_output…
├── tester_logger.py                  ← logging structuré (session.log, http_traffic, pdfdispatch)
├── conftest.py                       ← fixtures pytest (cfg, http, server, ui_page, wait_for_refresh…)
│
├── pytest.ini
├── requirements.txt                  ← requests, flask, playwright, pytest, pypdf…
└── tests/
    ├── test_00_generator.py          ← Phase 0  : auto-tests du générateur PDF
    ├── test_01_processing.py         ← Phase 1  : moteur de splitting (39 tests)
    ├── test_02_api.py                ← Phase 2  : API REST (55+ tests)
    ├── test_03_webhook.py            ← Phase 3  : webhook (19 tests)
    ├── test_04_email.py              ← Phase 4  : ingestion IMAP via Greenmail (16 tests)
    ├── test_06_security.py           ← Phase 6  : sécurité API (20 tests)
    ├── test_08_input_validation.py   ← Phase 7  : validation entrées (37 tests)
    └── test_09_ui.py                 ← Phase 9  : UI Playwright (smoke + triggers + options + email)
```

---

## Token GitHub

Le token d'accès GitHub utilisé par Portainer pour puller ce dépôt doit être **rotaté avant toute mise en public** du repo.
