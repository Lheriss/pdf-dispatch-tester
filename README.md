# pdf-dispatch-tester

Suite de tests d'intégration et end-to-end pour [pdf-dispatch](https://github.com/Lheriss/pdf-dispatch).

Les tests s'exécutent contre une **instance réelle et déployée** de pdf-dispatch.
Ils couvrent le moteur de traitement PDF, l'API REST, les webhooks, l'ingestion email et l'interface web.

---

## Phases

| Phase | Fichier | Ce qui est testé | Serveur | Filesystem |
|-------|---------|-----------------|---------|-----------|
| 0 | `test_00_generator.py` | Générateur de PDFs (auto-tests) | ❌ | ❌ |
| **1** | `test_01_processing.py` | **Moteur de splitting — placement × page_handling, glob, casse, adversarial, cas limites** | ✅ | ✅ |
| 2 | `test_02_api.py` | Endpoints REST | ✅ | ❌ |
| 3 | `test_03_webhook.py` | Webhook, payload, HMAC | ✅ | ❌ |
| 4 | `test_04_email.py` | Ingestion IMAP (SMTP automatique) | ✅ | ✅ |
| 5 | `test_05_ui.py` | Interface web (Playwright) | ✅ | ❌ |

---

## Architecture

```
NAS (même hôte)
├── Stack "pdf-dispatch-test"     image: ghcr.io/lheriss/pdf-dispatch:latest
│   └── port 5881
│   └── volume: /volume1/.../test-data → /data
│
└── Stack "pdf-dispatch-tester"   image: ghcr.io/lheriss/pdf-dispatch-tester:latest
    └── one-shot : s'arrête quand pytest se termine
    └── volume: /volume1/.../test-data → /data  ← même dossier
    └── volume: /volume1/.../logs → /app/logs
```

Les deux containers partagent le même `/data` sur l'hôte. Le tester écrit dans
`/data/input/`, pdf-dispatch traite, le tester lit `/data/output/` directement.

---

## Déploiement dans Portainer

### Stack 1 — `pdf-dispatch-test`

**Portainer → Stacks → Add stack → Repository**

| Champ | Valeur |
|-------|--------|
| Name | `pdf-dispatch-test` |
| Repository URL | `https://github.com/Lheriss/pdf-dispatch-tester` |
| Compose path | `docker-compose.test.yml` |

**Environment variables :**

| Variable | Exemple | Description |
|----------|---------|-------------|
| `PUID` | `1026` | `id -u <tonuser>` en SSH |
| `PGID` | `100` | `id -g <tonuser>` en SSH |
| `TZ` | `Europe/Zurich` | Fuseau horaire |
| `TEST_API_KEY` | `mon-test-key` | Clé API fixe (à choisir librement) |
| `EMAIL_SECRET_TEST` | *(openssl rand -hex 32)* | Clé chiffrement email |
| `DATA_VOLUME_TEST` | `/volume1/docker/pdf-dispatch-test/data` | Dossier data **isolé** de la production |

→ **Deploy the stack**

Vérifier : `http://ton-nas:5881` affiche l'interface pdf-dispatch.

---

### Stack 2 — `pdf-dispatch-tester`

**Portainer → Stacks → Add stack → Repository**

| Champ | Valeur |
|-------|--------|
| Name | `pdf-dispatch-tester` |
| Repository URL | `https://github.com/Lheriss/pdf-dispatch-tester` |
| Compose path | `docker-compose.tester.yml` |

**Environment variables :**

| Variable | Valeur | Description |
|----------|--------|-------------|
| `TEST_API_KEY` | `mon-test-key` | Même valeur que dans le stack 1 |
| `DATA_VOLUME_TEST` | `/volume1/docker/pdf-dispatch-test/data` | Même chemin que le stack 1 |
| `TESTER_LOGS` | `/volume1/docker/pdf-dispatch-tester/logs` | Où stocker les logs sur le NAS |
| `TESTER_REPORT` | `/volume1/docker/pdf-dispatch-tester/report` | Où stocker le rapport HTML |
| `PYTEST_ARGS` | `-v --tb=short` | Options pytest (voir tableau ci-dessous) |

→ **Deploy the stack**

Le container démarre, attend que pdf-dispatch soit prêt, lance pytest, puis s'arrête.

---

## Lancer les tests

**Portainer → stack `pdf-dispatch-tester` → container `pdf-dispatch-tester` → Recreate**

Le container repart de zéro à chaque Recreate : génère la config, lance pytest, s'arrête.

### Cibler une phase ou un test via `PYTEST_ARGS`

Modifier `PYTEST_ARGS` dans les variables d'environnement du stack, puis **Recreate** :

| `PYTEST_ARGS` | Effet |
|---------------|-------|
| `-v --tb=short` | Tous les tests |
| `-v tests/test_00_generator.py` | Phase 0 (sans serveur) |
| `-v -m processing` | Phase 1 — moteur de splitting |
| `-v -m "not email"` | Tout sauf email |
| `-v -k test_before_keep` | Un test précis par nom |
| `-v --tb=long` | Tracebacks complets pour debug |

---

## Consulter les résultats

**Logs en temps réel :**
Portainer → container `pdf-dispatch-tester` → icône **Logs**

**Logs détaillés (persistants après la fin du container) :**
Dans `TESTER_LOGS/<timestamp>/` sur le NAS :
```
session.log          ← log chronologique lisible
http_traffic.jsonl   ← chaque appel HTTP en JSON complet
pdfdispatch.log      ← journal de pdf-dispatch capturé pendant les tests
```

**Rapport HTML :**
`TESTER_REPORT/report.html` — accessible depuis le Mac via le partage réseau du NAS.

---

## (Optionnel) Phase 4 — Compte email de test

1. Créer un compte Gmail dédié (ex. `pdf-dispatch-test@gmail.com`)
2. Activer la validation en 2 étapes
3. Générer un mot de passe d'application : [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
4. Dans l'UI pdf-dispatch test (port 5881) : Options → Configurer l'email
5. Ajouter dans les variables du stack `pdf-dispatch-tester` :
   `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`, `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD`

---

## Structure du projet

```
pdf-dispatch-tester/
├── Dockerfile                       ← image ghcr.io/lheriss/pdf-dispatch-tester
├── docker-compose.test.yml          ← stack Portainer : instance pdf-dispatch test
├── docker-compose.tester.yml        ← stack Portainer : runner pytest
├── entrypoint.sh                    ← génère config.yaml depuis env vars, lance pytest
├── pdf_generator.py                 ← fabrique de PDFs (QR, Code128, adversarial)
├── file_dropper.py                  ← écrit /data/input/, lit /data/output/ (pypdf)
├── helpers.py                       ← upload_and_wait, set_config, assert_*
├── tester_logger.py                 ← logging structuré (3 fichiers par run)
├── conftest.py                      ← fixtures pytest
├── pytest.ini
├── requirements.txt
└── tests/
    ├── test_00_generator.py         ← Phase 0 : auto-tests (pas de serveur)
    ├── test_01_processing.py        ← Phase 1 : moteur de splitting
    └── ...                          ← Phases 2–5 à venir
```
