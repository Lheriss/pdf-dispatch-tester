# pdf-dispatch-tester

Suite de tests d'intégration et end-to-end pour [pdf-dispatch](https://github.com/Lheriss/pdf-dispatch).

Les tests s'exécutent contre une **instance réelle et déployée** de pdf-dispatch — pas de mocks.
Ils couvrent le moteur de traitement PDF (accès direct au filesystem), l'API REST, les webhooks,
l'ingestion email, et l'interface web.

---

## Phases

| Phase | Fichier | Ce qui est testé | Serveur | Filesystem |
|-------|---------|-----------------|---------|-----------|
| 0 | `test_00_generator.py` | Générateur de PDFs (auto-tests) | ❌ | ❌ |
| **1** | `test_01_processing.py` | **Moteur de splitting — 4 combinaisons placement×page_handling, glob, casse, adversarial, cas limites** | ✅ | ✅ |
| 2 | `test_02_api.py` | Tous les endpoints REST | ✅ | ❌ |
| 3 | `test_03_webhook.py` | Livraison webhook, payload, HMAC | ✅ | ❌ |
| 4 | `test_04_email.py` | Ingestion IMAP (envoi SMTP automatique) | ✅ | ✅ |
| 5 | `test_05_ui.py` | Interface web (Playwright) | ✅ | ❌ |

---

## Architecture de déploiement

```
NAS (même hôte)
├── Stack Portainer : pdf-dispatch-test    (port 5881)
│   └── container pdf-dispatch-test
│         volume: /volume1/.../test-data:/data
│
└── Stack Portainer : pdf-dispatch-tester  (one-shot)
    └── container pdf-dispatch-tester
          volume: /volume1/.../test-data:/data  ← même dossier
          volume: /volume1/.../pdf-dispatch-tester:/app
```

Les deux containers partagent le **même dossier `/data`** sur l'hôte.
Le tester écrit dans `/data/input/`, pdf-dispatch traite, le tester lit `/data/output/`.

---

## Déploiement dans Portainer

### Étape 1 — Cloner le repo sur le NAS

```bash
# En SSH sur le NAS
mkdir -p /volume1/docker/pdf-dispatch-tester
cd /volume1/docker/pdf-dispatch-tester
git clone https://github.com/Lheriss/pdf-dispatch-tester.git .
```

### Étape 2 — Stack `pdf-dispatch-test` (instance pdf-dispatch de test)

**Portainer → Stacks → Add stack**

| Champ | Valeur |
|-------|--------|
| Name | `pdf-dispatch-test` |
| Build method | Repository |
| Repository URL | `https://github.com/Lheriss/pdf-dispatch-tester` |
| Compose path | `docker-compose.test.yml` |

**Environment variables à définir dans Portainer :**

| Variable | Valeur | Description |
|----------|--------|-------------|
| `PUID` | résultat de `id -u <tonuser>` | UID propriétaire des fichiers |
| `PGID` | résultat de `id -g <tonuser>` | GID propriétaire des fichiers |
| `TZ` | `Europe/Zurich` | Fuseau horaire |
| `TEST_API_KEY` | `mon-test-key` | Clé API fixe pour les tests |
| `EMAIL_SECRET_TEST` | `openssl rand -hex 32` | Clé chiffrement email (test) |
| `DATA_VOLUME_TEST` | `/volume1/docker/pdf-dispatch-test/data` | Dossier data isolé |

→ **Deploy the stack**

Vérifier : `http://ton-nas:5881` doit afficher l'interface pdf-dispatch.

### Étape 3 — Stack `pdf-dispatch-tester` (runner de tests)

**Portainer → Stacks → Add stack**

| Champ | Valeur |
|-------|--------|
| Name | `pdf-dispatch-tester` |
| Build method | Upload (ou Repository) |
| Compose path | `docker-compose.tester.yml` |

**Environment variables à définir dans Portainer :**

| Variable | Valeur | Description |
|----------|--------|-------------|
| `TEST_API_KEY` | `mon-test-key` | Même clé que dans pdf-dispatch-test |
| `DATA_VOLUME_TEST` | `/volume1/docker/pdf-dispatch-test/data` | Même chemin que pdf-dispatch-test |
| `TESTER_SOURCE` | `/volume1/docker/pdf-dispatch-tester` | Chemin du repo cloné à l'étape 1 |
| `PYTEST_ARGS` | `-v --tb=short` | Options pytest (voir ci-dessous) |

→ **Deploy the stack**

Le container démarre, installe les dépendances, attend que pdf-dispatch soit prêt, lance pytest, puis s'arrête.

### Étape 4 — Lancer les tests

**Pour relancer les tests :**

Portainer → Stacks → `pdf-dispatch-tester` → container `pdf-dispatch-tester` → **Recreate**

Le container repart du début : install deps → wait → pytest → exit.

**Pour cibler une phase ou un test :**

Modifier `PYTEST_ARGS` dans les variables d'environnement du stack, puis Recreate :

| `PYTEST_ARGS` | Effet |
|---------------|-------|
| `-v --tb=short` | Tous les tests |
| `-v tests/test_00_generator.py` | Phase 0 seulement (sans serveur) |
| `-v -m processing` | Phase 1 seulement |
| `-v -m "not email"` | Tout sauf email |
| `-v -k test_before_keep` | Un test précis |
| `-v --tb=long` | Tracebacks complets pour debug |

### Étape 5 — Consulter les résultats

**Logs temps réel :**
Portainer → container `pdf-dispatch-tester` → **Logs** (icône en haut à droite)

**Logs détaillés (après le run) :**
Les fichiers sont dans `${TESTER_SOURCE}/logs/<timestamp>/` :
```
session.log          ← log chronologique lisible
http_traffic.jsonl   ← chaque appel HTTP en JSON complet
pdfdispatch.log      ← journal d'activité de pdf-dispatch capturé pendant les tests
```

**Rapport HTML :**
`${TESTER_SOURCE}/report/report.html` — ouvrir depuis le Mac via le partage réseau NAS.

---

## (Optionnel) Phase 4 — Compte email de test

Pour les tests d'ingestion IMAP, créer un compte Gmail dédié :

1. Créer `pdf-dispatch-test@gmail.com`
2. Activer la validation en 2 étapes
3. Générer un mot de passe d'application : [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
4. Dans l'UI pdf-dispatch (port 5881) : Options → Configurer l'email → ajouter la configuration
5. Ajouter dans les env vars du stack `pdf-dispatch-tester` :
   - `SMTP_HOST` = `smtp.gmail.com`
   - `SMTP_PORT` = `587`
   - `SMTP_USER` = `pdf-dispatch-test@gmail.com`
   - `SMTP_PASSWORD` = le mot de passe d'application
   - `IMAP_HOST` = `imap.gmail.com`, `IMAP_USER` = même adresse, `IMAP_PASSWORD` = même mot de passe

---

## Structure du projet

```
pdf-dispatch-tester/
├── docker-compose.test.yml      ← stack Portainer : instance pdf-dispatch test
├── docker-compose.tester.yml    ← stack Portainer : runner pytest
├── entrypoint.sh                ← génère config.yaml depuis env vars, lance pytest
├── pdf_generator.py             ← fabrique de PDFs de test (QR, Code128, adversarial)
├── file_dropper.py              ← écrit dans /data/input/, lit /data/output/ (pypdf)
├── helpers.py                   ← upload_and_wait, set_config, set_triggers, assert_*
├── tester_logger.py             ← logging structuré (session.log, http_traffic.jsonl)
├── conftest.py                  ← fixtures pytest (cfg, http, log, webhook_server)
├── config.yaml.example          ← template (auto-généré par entrypoint.sh en prod)
├── pytest.ini
├── requirements.txt
└── tests/
    ├── test_00_generator.py     ← Phase 0 : auto-tests du générateur PDF
    ├── test_01_processing.py    ← Phase 1 : moteur de splitting (filesystem)
    ├── test_02_api.py           ← Phase 2 : REST API (à venir)
    ├── test_03_webhook.py       ← Phase 3 : webhook (à venir)
    ├── test_04_email.py         ← Phase 4 : ingestion IMAP (à venir)
    └── test_05_ui.py            ← Phase 5 : interface web Playwright (à venir)
```
