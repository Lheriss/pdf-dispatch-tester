"""
web_runner.py — Minimal web interface for pdf-dispatch-tester.
"""
from __future__ import annotations
import io, json, os, subprocess, threading, time, zipfile
from datetime import datetime
from pathlib import Path
from flask import Flask, Response, jsonify, render_template_string, request, send_file
from werkzeug.utils import secure_filename

app    = Flask(__name__)
SERVER = os.environ.get("TESTER_SERVER", "http://localhost:5000")
PORT      = int(os.environ.get("WEB_PORT", 5883))
DATA_PATH = Path(os.environ.get("TESTER_DATA", "/data"))
BASE_PDF  = DATA_PATH / "base_content.pdf"
BASE_META = DATA_PATH / "base_content_meta.json" 

GROUPS = [
    # ─── Phase 0 ─── Générateur de fixtures PDF ───────────────────────────
    {
        "id": "phase0",
        "label": "Phase 0 — Générateur PDF",
        "desc": "Génération de fixtures PDF (outil interne) · 19 tests — lancé automatiquement",
        "args": ["tests/test_00_generator.py"],
        "available": False,
    },
    # ─── Phase 1 ─── Traitement core (watchdog) ───────────────────────────
    {
        "id": "phase1",
        "label": "Phase 1 — Traitement core (watchdog)",
        "desc": "Pipeline de traitement via dépôt fichier · 39 tests",
        "args": [],
        "available": True,
    },
    {
        "id": "phase1_placement",
        "label": "↳ Placement × page_handling",
        "desc": "Before/After × Keep/Delete · 8 tests",
        "args": [
            "tests/test_01_processing.py::TestBeforeKeep",
            "tests/test_01_processing.py::TestBeforeDelete",
            "tests/test_01_processing.py::TestAfterKeep",
            "tests/test_01_processing.py::TestAfterDelete",
        ],
        "available": True, "sub": True, "parent": "phase1",
    },
    {
        "id": "phase1_triggers",
        "label": "↳ Matching de déclencheurs",
        "desc": "Exact, glob, insensible à la casse, permissif",
        "args": ["tests/test_01_processing.py::TestTriggerMatching"],
        "available": True, "sub": True, "parent": "phase1",
    },
    {
        "id": "phase1_multi",
        "label": "↳ Multi-déclencheurs",
        "desc": "Plusieurs triggers dans un même PDF",
        "args": ["tests/test_01_processing.py::TestMultiTrigger"],
        "available": True, "sub": True, "parent": "phase1",
    },
    {
        "id": "phase1_adversarial",
        "label": "↳ Fichiers adversariaux",
        "desc": "PDFs invalides, corrompus, faux formats",
        "args": ["tests/test_01_processing.py::TestAdversarial"],
        "available": True, "sub": True, "parent": "phase1",
    },
    {
        "id": "phase1_edge",
        "label": "↳ Cas limites",
        "desc": "Fichiers volumineux, trop de pages, noms longs",
        "args": ["tests/test_01_processing.py::TestEdgeCases"],
        "available": True, "sub": True, "parent": "phase1",
    },
    # ─── Phase 2 ─── API REST ─────────────────────────────────────────────
    {
        "id": "phase2",
        "label": "Phase 2 — API REST",
        "desc": "Upload, tâches, configuration via API · 55 tests",
        "args": [],
        "available": True,
    },
    {
        "id": "phase2_upload",
        "label": "↳ Placement × page_handling",
        "desc": "Before/After × Keep/Delete via API · 12 tests",
        "args": [
            "tests/test_02_api.py::TestApiBeforeKeep",
            "tests/test_02_api.py::TestApiBeforeDelete",
            "tests/test_02_api.py::TestApiAfterKeep",
            "tests/test_02_api.py::TestApiAfterDelete",
        ],
        "available": True, "sub": True, "parent": "phase2",
    },
    {
        "id": "phase2_config",
        "label": "↳ Config overrides & triggers",
        "desc": "Per-file overrides + matching · 9 tests",
        "args": [
            "tests/test_02_api.py::TestApiConfigOverride",
            "tests/test_02_api.py::TestApiTriggerMatching",
        ],
        "available": True, "sub": True, "parent": "phase2",
    },
    {
        "id": "phase2_lifecycle",
        "label": "↳ Auth & lifecycle",
        "desc": "Auth, task IDs, task list · 10 tests",
        "args": [
            "tests/test_02_api.py::TestApiAuth",
            "tests/test_02_api.py::TestApiTaskLifecycle",
        ],
        "available": True, "sub": True, "parent": "phase2",
    },
    {
        "id": "phase2_errors",
        "label": "↳ Erreurs & cas limites",
        "desc": "Fichiers invalides, champs manquants · 5 tests",
        "args": ["tests/test_02_api.py::TestApiErrors"],
        "available": True, "sub": True, "parent": "phase2",
    },
    {
        "id": "phase2_security",
        "label": "↳ Sécurité & détournements",
        "desc": "Auth bypass, injection, SSRF, payloads · 24 tests",
        "args": [
            "tests/test_02_api.py::TestApiAuthBypass",
            "tests/test_02_api.py::TestApiFilenameInjection",
            "tests/test_02_api.py::TestApiConfigInjection",
            "tests/test_02_api.py::TestApiSsrf",
            "tests/test_02_api.py::TestApiMaliciousPayload",
        ],
        "available": True, "sub": True, "parent": "phase2",
    },
    # ─── Phase 3 ─── Webhooks ─────────────────────────────────────────────
    {
        "id": "phase3",
        "label": "Phase 3 — Webhooks",
        "desc": "Payload, HMAC, filtrage, livraison, retry · 19 tests",
        "args": [],
        "available": True,
    },
    {
        "id": "phase3_payload",
        "label": "↳ Structure du payload",
        "desc": "Champs obligatoires, types, événements",
        "args": ["tests/test_03_webhook.py::TestWebhookPayloadStructure"],
        "available": True, "sub": True, "parent": "phase3",
    },
    {
        "id": "phase3_hmac",
        "label": "↳ Signature HMAC-SHA256",
        "desc": "Présence, calcul, vérification",
        "args": ["tests/test_03_webhook.py::TestWebhookHmac"],
        "available": True, "sub": True, "parent": "phase3",
    },
    {
        "id": "phase3_filter",
        "label": "↳ Filtrage par type d'événement",
        "desc": "success / error / all",
        "args": ["tests/test_03_webhook.py::TestWebhookFilter"],
        "available": True, "sub": True, "parent": "phase3",
    },
    {
        "id": "phase3_delivery",
        "label": "↳ Livraison & gardes",
        "desc": "URL vide, webhook désactivé",
        "args": ["tests/test_03_webhook.py::TestWebhookDelivery"],
        "available": True, "sub": True, "parent": "phase3",
    },
    {
        "id": "phase3_retry",
        "label": "↳ Retry sur 5xx (lent)",
        "desc": "Relance automatique · 1 test (~30 s)",
        "args": ["tests/test_03_webhook.py::TestWebhookRetry"],
        "available": True, "sub": True, "parent": "phase3",
    },
    # ─── Phase 4 ─── Email IMAP (Greenmail) ───────────────────────────────
    {
        "id": "phase4",
        "label": "Phase 4 — Email IMAP",
        "desc": "Configuration, pipeline, limites ressources · 16 tests",
        "args": [],
        "available": True,
    },
    {
        "id": "phase4_config",
        "label": "↳ Configuration email",
        "desc": "CRUD configs, test de connexion · 6 tests",
        "args": ["tests/test_04_email.py::TestEmailConfigAPI"],
        "available": True, "sub": True, "parent": "phase4",
    },
    {
        "id": "phase4_processing",
        "label": "↳ Pipeline de traitement",
        "desc": "Réception, traitement, filtres expéditeur/sujet · 8 tests",
        "args": ["tests/test_04_email.py::TestEmailProcessing"],
        "available": True, "sub": True, "parent": "phase4",
    },
    {
        "id": "phase4_limits",
        "label": "↳ Limites ressources (email)",
        "desc": "Pièce jointe trop lourde, trop de pages · 2 tests",
        "args": ["tests/test_04_email.py::TestEmailLimits"],
        "available": True, "sub": True, "parent": "phase4",
    },
    # ─── Phase 5 ─── Sécurité ─────────────────────────────────────────────
    {
        "id": "phase5",
        "label": "Phase 5 — Sécurité",
        "desc": "Injection config, exposition de secrets · 20 tests",
        "args": [],
        "available": True,
    },
    {
        "id": "phase5_config_poison",
        "label": "↳ Injection config (POST /api/config)",
        "desc": "email_configs, stats, counter, dirs · 12 tests",
        "args": ["tests/test_05_security.py::TestConfigPoisoning"],
        "available": True, "sub": True, "parent": "phase5",
    },
    {
        "id": "phase5_secrets",
        "label": "↳ Exposition password_enc",
        "desc": "Absence de password_enc dans les réponses API · 8 tests",
        "args": ["tests/test_05_security.py::TestPasswordEncExposure"],
        "available": True, "sub": True, "parent": "phase5",
    },
    # ─── Phase 6 ─── Validation des entrées ───────────────────────────────
    {
        "id": "phase6",
        "label": "Phase 6 — Validation des entrées",
        "desc": "CRLF IMAP, bornes port/interval, barcode→fichier, log injection, n invalide · 37 tests",
        "args": [],
        "available": True,
    },
    {
        "id": "phase6_crlf",
        "label": "↳ Injection CRLF (champs IMAP)",
        "desc": "host, username, folder — create + update · 7 tests",
        "args": ["tests/test_06_input_validation.py::TestCrlfInjection"],
        "available": True, "sub": True, "parent": "phase6",
    },
    {
        "id": "phase6_bounds",
        "label": "↳ Bornes port & poll_interval",
        "desc": "port 0/-1/>65535, poll_interval ≤0 · 11 tests",
        "args": ["tests/test_06_input_validation.py::TestEmailConfigBounds"],
        "available": True, "sub": True, "parent": "phase6",
    },
    {
        "id": "phase6_barcode",
        "label": "↳ Barcode → nom de fichier",
        "desc": "Path traversal, valeur très longue · 2 tests",
        "args": ["tests/test_06_input_validation.py::TestBarcodeFilenameRegression"],
        "available": True, "sub": True, "parent": "phase6",
    },
    {
        "id": "phase6_log",
        "label": "↳ Injection dans /api/log",
        "desc": "CRLF, ANSI, null byte, message légitime · 5 tests",
        "args": ["tests/test_06_input_validation.py::TestLogInjection"],
        "available": True, "sub": True, "parent": "phase6",
    },
    {
        "id": "phase6_n_param",
        "label": "↳ Paramètre n invalide (/api/recent)",
        "desc": "n=abc → 400, n=3.14 → 400, n vide, bornes · 5 tests",
        "args": ["tests/test_06_input_validation.py::TestApiRecentNParam"],
        "available": True, "sub": True, "parent": "phase6",
    },
    # ─── Phase 7 ─── Interface utilisateur (Playwright) ───────────────────
    {
        "id": "phase7",
        "label": "Phase 7 — Interface utilisateur (UI)",
        "desc": "Tests Playwright Chromium headless · 28 tests",
        "args": [],
        "available": True,
    },
    {
        "id": "phase7_smoke",
        "label": "↳ Smoke — infrastructure de base",
        "desc": "Chargement, JS, i18n, stats, version · 7 tests",
        "args": ["tests/test_07_ui.py::TestUiSmoke"],
        "available": True, "sub": True, "parent": "phase7",
    },
    {
        "id": "phase7_triggers",
        "label": "↳ Triggers CRUD",
        "desc": "Ajout, suppression, persistance, doublons · 9 tests",
        "args": ["tests/test_07_ui.py::TestUiTriggers"],
        "available": True, "sub": True, "parent": "phase7",
    },
    {
        "id": "phase7_options",
        "label": "↳ Options",
        "desc": "Séparateur, suppression source · 4 tests",
        "args": ["tests/test_07_ui.py::TestUiOptions"],
        "available": True, "sub": True, "parent": "phase7",
    },
    {
        "id": "phase7_email",
        "label": "↳ Panneau email — régressions UI",
        "desc": "Radios, SSL, dropdown triggers · 8 tests",
        "args": ["tests/test_07_ui.py::TestUiEmailPanel"],
        "available": True, "sub": True, "parent": "phase7",
    },
    # ─── Phase 7d ─── Séparateur ──────────────────────────────────────────
    {
        "id": "phase7_separator",
        "label": "↳ Séparateur — radios et exclusion mutuelle",
        "desc": "Heading i18n, 2 radios, exclusion mutuelle, labels traduits · 5 tests",
        "args": ["tests/test_07_ui.py::TestUiSeparator"],
        "available": True, "sub": True, "parent": "phase7",
    },
    # ─── Phase 7g ─── Webhook ──────────────────────────────────────────────
    {
        "id": "phase7_webhook",
        "label": "↳ Webhook HTTP — panneau et persistance",
        "desc": "Toggle, config masquée/visible, events select, URL, persistance · 10 tests",
        "args": ["tests/test_07_ui.py::TestUiWebhook"],
        "available": True, "sub": True, "parent": "phase7",
    },
    # ─── Phase 7h/7i/7j ─── Dirs, ApiKey, Tokens ──────────────────────────────
    {
        "id": "phase7_dirs",
        "label": "↳ Configurer les dossiers",
        "desc": "Panneau Dirs : chemin racine, table repertoires, i18n · 4 tests",
        "args": ["tests/test_07_ui.py::TestUiDirs"],
        "available": True, "sub": True, "parent": "phase7",
    },
    {
        "id": "phase7_apikey",
        "label": "↳ Cle API",
        "desc": "Presence, masquage par defaut, toggle show/hide · 6 tests",
        "args": ["tests/test_07_ui.py::TestUiApiKey"],
        "available": True, "sub": True, "parent": "phase7",
    },
    {
        "id": "phase7_tokens",
        "label": "↳ Tokens de nom de fichier",
        "desc": "Liste, separateurs, ajout texte libre · 5 tests",
        "args": ["tests/test_07_ui.py::TestUiFilenameTokens"],
        "available": True, "sub": True, "parent": "phase7",
    },
    # ─── Phase 8 ─── File-drop avancé ──────────────────────────────────────
    {
        "id": "phase8",
        "label": "Phase 8 — File-drop avancé",
        "desc": "Vérification page-count, corruption config, robustesse répertoires · 15 tests",
        "args": [],
        "available": True,
    },
    {
        "id": "phase8_pagecounts",
        "label": "↳ Vérification page-count (API + filesystem)",
        "desc": "Dual-check événements /api/state + pypdf sur les fichiers produits · 6 tests",
        "args": ["tests/test_08_filedrop.py::TestFiledropPageCounts"],
        "available": True, "sub": True, "parent": "phase8",
    },
    {
        "id": "phase8_config",
        "label": "↳ Corruption de .splitter_config.json",
        "desc": "Config manquante, JSON invalide, types incorrects, restauration API · 5 tests",
        "args": ["tests/test_08_filedrop.py::TestConfigCorruption"],
        "available": True, "sub": True, "parent": "phase8",
    },
    {
        "id": "phase8_dirs",
        "label": "↳ Robustesse des répertoires",
        "desc": "Renommage trigger subdir, suppression no_code/ et error/ · 4 tests",
        "args": ["tests/test_08_filedrop.py::TestDirectoryRobustness"],
        "available": True, "sub": True, "parent": "phase8",
    },    },
    {
        "id": "phase8_persistence",
        "label": "↳ Persistance config sur disque",
        "desc": "Chaque changement API ecrit dans .splitter_config.json · 5 tests",
        "args": ["tests/test_08_filedrop.py::TestConfigPersistence"],
        "available": True, "sub": True, "parent": "phase8",
    },
    # ─── Phase 2 — supplementaire ────────────────────────────────────────────
    {
        "id": "phase2_concurrent",
        "label": "↳ Uploads concurrents",
        "desc": "2-3 taches soumises sans attente, IDs uniques, docs correct · 4 tests",
        "args": ["tests/test_02_api.py::TestApiConcurrentUploads"],
        "available": True, "sub": True, "parent": "phase2",
    },
    {
        "id": "phase2_error_format",
        "label": "↳ Format uniforme des erreurs",
        "desc": "{ok:false, error:...} sur tous les endpoints d'erreur · 6 tests",
        "args": ["tests/test_02_api.py::TestApiErrorFormat"],
        "available": True, "sub": True, "parent": "phase2",
    },
    # ─── Phase 4 — supplementaire ────────────────────────────────────────────
    {
        "id": "phase4_password",
        "label": "↳ Roundtrip mot de passe email",
        "desc": "password_enc stocke + non expose + connexion OK · 4 tests",
        "args": ["tests/test_04_email.py::TestEmailPasswordRoundtrip"],
        "available": True, "sub": True, "parent": "phase4",
    },
    {
        "id": "phase4_dedup",
        "label": "↳ Deduplication processed_ids",
        "desc": "Pas de double traitement + reset_ids · 3 tests",
        "args": ["tests/test_04_email.py::TestEmailDeduplication"],
        "available": True, "sub": True, "parent": "phase4",
    },
    {
        "id": "phase4_filesystem",
        "label": "↳ Integration email → filesystem",
        "desc": "Verification croisee API + pypdf page-count · 3 tests",
        "args": ["tests/test_04_email.py::TestEmailFilesystemIntegration"],
        "available": True, "sub": True, "parent": "phase4",
    },
]