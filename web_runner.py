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
    {
        "id": "phase0",
        "label": "Phase 0 — Générateur PDF",
        "desc": "19 tests autonomes · aucun serveur requis",
        "args": ["tests/test_00_generator.py"],
        "available": True,
    },
    {
        "id": "phase1",
        "label": "Phase 1 — Traitement core (watchdog)",
        "desc": "39 tests de fractionnement via dépôt fichier",
        "args": [],   # parent uniquement — commande pilotée par les sous-groupes
        "available": True,
    },
    {
        "id": "phase1_placement",
        "label": "↳ Placement × page_handling",
        "desc": "Before/After × Keep/Delete · 17 tests",
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
        "desc": "Exact, glob, casse, permissif · 8 tests",
        "args": ["tests/test_01_processing.py::TestTriggerMatching"],
        "available": True, "sub": True, "parent": "phase1",
    },
    {
        "id": "phase1_multi",
        "label": "↳ Multi-déclencheurs",
        "desc": "Séquences et même page · 4 tests",
        "args": ["tests/test_01_processing.py::TestMultiTrigger"],
        "available": True, "sub": True, "parent": "phase1",
    },
    {
        "id": "phase1_adversarial",
        "label": "↳ Fichiers adversariaux",
        "desc": "Corrompus, zéro-octet, faux PDF, limites MB/pages · 8 tests",
        "args": ["tests/test_01_processing.py::TestAdversarial"],
        "available": True, "sub": True, "parent": "phase1",
    },
    {
        "id": "phase1_edge",
        "label": "↳ Cas limites",
        "desc": "Page unique, code en dernière page · 4 tests",
        "args": ["tests/test_01_processing.py::TestEdgeCases"],
        "available": True, "sub": True, "parent": "phase1",
    },
    {
        "id": "phase2",
        "label": "Phase 2 — API REST",
        "desc": "40 tests upload, tâches, config, sécurité",
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
        "desc": "Auth bypass, injection, SSRF, payloads, limites · 27 tests",
        "args": [
            "tests/test_02_api.py::TestApiAuthBypass",
            "tests/test_02_api.py::TestApiFilenameInjection",
            "tests/test_02_api.py::TestApiConfigInjection",
            "tests/test_02_api.py::TestApiSsrf",
            "tests/test_02_api.py::TestApiMaliciousPayload",
        ],
        "available": True, "sub": True, "parent": "phase2",
    },
    {
        "id": "phase3",
        "label": "Phase 3 — Webhooks",
        "desc": "19 tests · payload, HMAC-SHA256, filtrage événements, retry",
        "args": [],
        "available": True,
    },
    {
        "id": "phase3_payload",
        "label": "↳ Structure du payload",
        "desc": "8 champs obligatoires, types, valeurs success & error · 8 tests",
        "args": ["tests/test_03_webhook.py::TestWebhookPayloadStructure"],
        "available": True, "sub": True, "parent": "phase3",
    },
    {
        "id": "phase3_hmac",
        "label": "↳ Signature HMAC-SHA256",
        "desc": "Absent sans secret · présent et vérifiable avec secret · 3 tests",
        "args": ["tests/test_03_webhook.py::TestWebhookHmac"],
        "available": True, "sub": True, "parent": "phase3",
    },
    {
        "id": "phase3_filter",
        "label": "↳ Filtrage par type d'événement",
        "desc": "all / success / error — livraison et suppression · 6 tests",
        "args": ["tests/test_03_webhook.py::TestWebhookFilter"],
        "available": True, "sub": True, "parent": "phase3",
    },
    {
        "id": "phase3_delivery",
        "label": "↳ Livraison & gardes",
        "desc": "webhook_enabled=False, URL vide · 2 tests",
        "args": ["tests/test_03_webhook.py::TestWebhookDelivery"],
        "available": True, "sub": True, "parent": "phase3",
    },
    {
        "id": "phase3_retry",
        "label": "↳ Retry sur 5xx (lent)",
        "desc": "Réessai sur HTTP 503 avec receiver inline · 1 test",
        "args": ["tests/test_03_webhook.py::TestWebhookRetry"],
        "available": True, "sub": True, "parent": "phase3",
    },
    {
        "id": "phase4",
        "label": "Phase 4 — Email IMAP",
        "desc": "16 tests — config CRUD + pipeline complet SMTP→IMAP + limites",
        "args": [],
        "available": True,
    },
    {
        "id": "phase4_config",
        "label": "↳ Configuration email",
        "desc": "CRUD /api/email/configs + test connexion · 6 tests",
        "args": ["tests/test_04_email.py::TestEmailConfigAPI"],
        "available": True, "sub": True, "parent": "phase4",
    },
    {
        "id": "phase4_processing",
        "label": "↳ Pipeline de traitement",
        "desc": "SMTP→Greenmail→IMAP→output · 8 tests",
        "args": ["tests/test_04_email.py::TestEmailProcessing"],
        "available": True, "sub": True, "parent": "phase4",
    },
    {
        "id": "phase4_limits",
        "label": "↳ Limites ressources (email)",
        "desc": "MAX_UPLOAD_MB et MAX_PAGES via email · 2 tests",
        "args": ["tests/test_04_email.py::TestEmailLimits"],
        "available": True, "sub": True, "parent": "phase4",
    },
    {
        "id": "phase5",
        "label": "Phase 5 — Sécurité",
        "desc": "20 tests · injection config, password_enc, traversée dirs",
        "args": [],
        "available": True,
    },
    {
        "id": "phase5_config_poison",
        "label": "↳ Injection config (POST /api/config)",
        "desc": "email_configs, stats, counter, dirs traversal · 12 tests",
        "args": ["tests/test_06_security.py::TestConfigPoisoning"],
        "available": True, "sub": True, "parent": "phase5",
    },
    {
        "id": "phase5_secrets",
        "label": "↳ Exposition password_enc",
        "desc": "password_enc absent des réponses create/update/state · 8 tests",
        "args": ["tests/test_06_security.py::TestPasswordEncExposure"],
        "available": True, "sub": True, "parent": "phase5",
    },
    {
        "id": "phase6",
        "label": "Phase 6 — File-drop avancé",
        "desc": "Tests de robustesse filesystem (à venir)",
        "args": ["-m", "filedrop"], "available": False,
    },
]

_lock = threading.Lock()
_job: dict | None = None

_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>pdf-dispatch · tester</title>
<style>
  :root{--bg:#0f1117;--panel:#1a1d27;--border:#2d3147;--text:#e2e4f0;
        --muted:#6b7280;--accent:#4f9eff;--green:#34d399;--red:#f87171;
        --yellow:#fbbf24;}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;
       font-size:14px;line-height:1.5}
  .wrap{max-width:1100px;margin:0 auto;padding:24px 20px}
  .cols{display:grid;grid-template-columns:340px 1fr;gap:20px;align-items:start}
  header{display:flex;align-items:baseline;gap:16px;border-bottom:1px solid var(--border);
         padding-bottom:16px;margin-bottom:24px}
  header h1{font-size:20px;font-weight:700;color:var(--accent)}
  .srv{font-size:12px;color:var(--muted);font-family:monospace}
  #health{font-size:11px;padding:2px 8px;border-radius:999px;background:var(--panel);
          border:1px solid var(--border);margin-left:auto}
  .panel{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:16px}
  .panel h2{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;
             color:var(--muted);margin-bottom:12px}
  .group{display:flex;align-items:flex-start;gap:10px;padding:8px 6px;border-radius:6px;
         cursor:pointer;transition:background .1s}
  .group:hover:not(.disabled){background:rgba(79,158,255,.07)}
  .group.disabled{opacity:.38;cursor:not-allowed}
  .group.sub label{font-size:13px}
  .group.sub{padding-left:24px}
  .group input[type=checkbox]{margin-top:3px;accent-color:var(--accent);
                               width:15px;height:15px;flex-shrink:0}
  .group label{cursor:inherit}
  .group label strong{display:block}
  .group label small{color:var(--muted);font-size:12px}
  .divider{height:1px;background:var(--border);margin:8px 0}
  .quick{display:flex;gap:6px;margin-bottom:14px;flex-wrap:wrap}
  .quick button{font-size:11px;padding:3px 10px;border-radius:4px;border:1px solid var(--border);
                background:transparent;color:var(--muted);cursor:pointer;transition:all .15s}
  .quick button:hover{border-color:var(--accent);color:var(--accent)}
  #launch{width:100%;margin-top:16px;padding:10px;background:var(--accent);color:#fff;
          border:none;border-radius:6px;font-size:14px;font-weight:600;cursor:pointer;
          transition:opacity .15s}
  #launch:hover{opacity:.85}
  #launch:disabled{opacity:.35;cursor:not-allowed}
  #status-bar{display:none;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap}
  #status-badge{font-size:12px;font-weight:600;padding:3px 10px;border-radius:999px;border:1px solid}
  .badge-running{color:var(--yellow);border-color:var(--yellow);background:rgba(251,191,36,.08)}
  .badge-passed{color:var(--green);border-color:var(--green);background:rgba(52,211,153,.08)}
  .badge-failed{color:var(--red);border-color:var(--red);background:rgba(248,113,113,.08)}
  .badge-stopped{color:var(--yellow);border-color:var(--yellow);background:rgba(251,191,36,.08)}
  #elapsed{color:var(--muted);font-size:12px;font-family:monospace}
  .top-links{margin-left:auto;display:flex;gap:12px;align-items:center}
  .top-links a{font-size:12px;color:var(--accent);text-decoration:none}
  .top-links a:hover{text-decoration:underline}
  #out{width:100%;height:560px;background:#090b10;border:1px solid var(--border);
       border-radius:6px;padding:12px 14px;font-family:monospace;font-size:12.5px;
       line-height:1.6;overflow-y:auto;white-space:pre-wrap;word-break:break-word;
       color:#c9d1d9;display:none}
  .ln-pass{color:var(--green)}
  .ln-fail{color:var(--red)}
  .ln-error{color:var(--red);font-weight:600}
  .ln-sep{color:#3d4560}
  .ln-head{color:var(--accent)}
  #idle{color:var(--muted);font-size:13px;margin-top:40px;text-align:center}
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>pdf&#8209;dispatch&thinsp;·&thinsp;tester</h1>
  <span class="srv">{{ server }}</span>
  <span id="health">⏳ vérification…</span>
</header>
<div class="cols">

  <div>
    <div class="panel">
      <h2>Tests à lancer</h2>
      <div class="quick">
        <button onclick="selAll()">Tout disponible</button>
        <button onclick="selPhase(0)">Phase 0</button>
        <button onclick="selPhase(1)">Phase 1</button>
        <button onclick="selNone()">Aucun</button>
      </div>
      {% for g in groups %}
        {% if g.id == 'phase1_placement' %}<div class="divider"></div>{% endif %}
        {% if g.id == 'phase2' %}<div class="divider"></div>{% endif %}
        {% if g.id == 'phase3' %}<div class="divider"></div>{% endif %}
        {% if g.id == 'phase4' %}<div class="divider"></div>{% endif %}
        {% if g.id == 'phase5' %}<div class="divider"></div>{% endif %}
        {% if g.id == 'phase6' %}<div class="divider"></div>{% endif %}
        <div class="group {% if g.get('sub') %}sub {% endif %}{% if not g.available %}disabled{% endif %}"
             data-id="{{ g.id }}"
             {% if g.get('parent') %}data-parent="{{ g.get('parent') }}"{% endif %}
             onclick="divClick(event,this)">
          <input type="checkbox" id="{{ g.id }}" value="{{ g.id }}"
                 {% if g.available %}checked{% endif %}
                 {% if not g.available %}disabled{% endif %}>
          <label for="{{ g.id }}">
            <strong>{{ g.label }}</strong>
            <small>{{ g.desc }}</small>
          </label>
        </div>
      {% endfor %}
      <button id="launch" onclick="launch()">▶ Lancer les tests</button>
      <button id="stop-btn" onclick="stopTests()" style="display:none;background:#7f1d1d;border-color:#ef4444;margin-left:8px">&#9209; Arrêter</button>
    </div>

    <div class="panel" style="margin-top:16px">
      <h2>Document de base (optionnel)</h2>
      <p style="color:var(--muted);font-size:12px;margin-bottom:10px">
        PDF fourni comme contenu des pages « document » dans les fixtures de test.
        Le tester y insère les pages de code-barres aux positions requises.
      </p>
      <div id="base-info" style="display:none;font-size:12px;margin-bottom:8px;color:var(--green)">
        📄 <span id="base-name"></span> · <span id="base-pages"></span> pages
        <button onclick="clearBase()"
                style="margin-left:8px;font-size:11px;border:1px solid var(--border);
                       background:transparent;color:var(--muted);border-radius:3px;cursor:pointer;padding:1px 6px">
          ✕
        </button>
      </div>
      <input type="file" id="base-file" accept=".pdf" style="display:none" onchange="uploadBase()">
      <button onclick="document.getElementById('base-file').click()"
              style="font-size:12px;padding:4px 12px;border:1px solid var(--border);
                     background:transparent;color:var(--text);border-radius:4px;cursor:pointer">
        📂 Choisir un PDF…
      </button>
      <div id="upload-err" style="color:var(--red);font-size:12px;margin-top:6px;display:none"></div>
    </div>
  </div>

  <div>
    <div id="status-bar">
      <span id="status-badge">…</span>
      <span id="elapsed"></span>
      <div class="top-links">
        <a id="report-link" href="#" style="display:none" target="_blank">📄 Rapport HTML</a>
        <a id="dl-link" href="/download" style="display:none">📥 Télécharger tout</a>
      </div>
    </div>
    <div id="out"></div>
    <p id="idle">Sélectionnez les tests et cliquez sur « Lancer les tests ».</p>
  </div>

</div>
</div>
<script>
// ── helpers ──────────────────────────────────────────────────────────────────
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function colorLine(l){
  if(/ PASSED/.test(l))return'<span class="ln-pass">'+esc(l)+'</span>';
  if(/ FAILED/.test(l)||/^FAILED/.test(l))return'<span class="ln-fail">'+esc(l)+'</span>';
  if(/^(ERROR|E\\s)/.test(l))return'<span class="ln-error">'+esc(l)+'</span>';
  if(/^[=\\-]{5,}/.test(l))return'<span class="ln-sep">'+esc(l)+'</span>';
  if(/^(platform|rootdir|plugins|collecting|testpaths)/.test(l))return'<span class="ln-head">'+esc(l)+'</span>';
  return esc(l);
}

// ── health ───────────────────────────────────────────────────────────────────
async function checkHealth(){
  const el=document.getElementById('health');
  try{const r=await fetch('/healthz-proxy');
    if(r.ok){el.textContent='🟢 pdf-dispatch OK';el.style.color='var(--green)'}
    else{el.textContent='🔴 hors ligne';el.style.color='var(--red)'}}
  catch{el.textContent='🔴 injoignable';el.style.color='var(--red)'}
}
checkHealth();setInterval(checkHealth,30000);

// ── parent-child checkbox sync ────────────────────────────────────────────────
function syncParent(parentId) {
  const p = document.getElementById(parentId); if (!p) return;
  const ch = [...document.querySelectorAll(`[data-parent="${parentId}"] input[type=checkbox]`)];
  if (!ch.length) return;
  const all = ch.every(c => c.checked), none = ch.every(c => !c.checked);
  p.checked = all; p.indeterminate = !all && !none;
}

function propagate(div, checked) {
  // Push state to all children
  document.querySelectorAll(`[data-parent="${div.dataset.id}"] input[type=checkbox]`).forEach(c => {
    c.checked = checked; c.indeterminate = false;
  });
  // Update grandparent if this is a child
  if (div.dataset.parent) syncParent(div.dataset.parent);
}

// Called when user clicks anywhere on a group row
function divClick(event, div) {
  if (div.classList.contains('disabled')) return;
  const cb = div.querySelector('input[type=checkbox]');
  if (!cb || cb.disabled) return;
  // If click landed ON the checkbox, it already toggled — just propagate
  // If click landed elsewhere (div/label), toggle first
  if (event.target !== cb) cb.checked = !cb.checked;
  cb.indeterminate = false;
  propagate(div, cb.checked);
}

// Init: sync all parents on page load
document.querySelectorAll('.group[data-id]:not([data-parent])').forEach(div => {
  syncParent(div.dataset.id);
});

// ── selection shortcuts ───────────────────────────────────────────────────────
function selAll() {
  document.querySelectorAll('input[type=checkbox]:not(:disabled)').forEach(c => {
    c.checked = true; c.indeterminate = false;
  });
}
function selNone() {
  document.querySelectorAll('input[type=checkbox]:not(:disabled)').forEach(c => {
    c.checked = false; c.indeterminate = false;
  });
}
function selPhase(n) {
  selNone();
  document.querySelectorAll('.group[data-id]').forEach(div => {
    const cb = div.querySelector('input[type=checkbox]');
    if (!cb || cb.disabled) return;
    if (div.dataset.id.startsWith('phase' + n)) {
      cb.checked = true; cb.indeterminate = false;
      propagate(div, true);
    }
  });
  document.querySelectorAll('.group[data-id]:not([data-parent])').forEach(div => syncParent(div.dataset.id));
}

// ── base PDF upload ───────────────────────────────────────────────────────────
async function uploadBase() {
  const file = document.getElementById('base-file').files[0];
  if (!file) return;
  const form = new FormData(); form.append('file', file);
  const r = await fetch('/upload-base', {method: 'POST', body: form});
  const d = await r.json();
  const errEl = document.getElementById('upload-err');
  if (r.ok) {
    document.getElementById('base-name').textContent = d.name;
    document.getElementById('base-pages').textContent = d.pages;
    document.getElementById('base-info').style.display = 'block';
    errEl.style.display = 'none';
  } else {
    errEl.textContent = d.error || 'Erreur upload'; errEl.style.display = 'block';
  }
}
async function clearBase() {
  await fetch('/upload-base', {method: 'DELETE'});
  document.getElementById('base-info').style.display = 'none';
  document.getElementById('base-file').value = '';
}
// Check on load
fetch('/base-info').then(r => r.json()).then(d => {
  if (d.name) {
    document.getElementById('base-name').textContent = d.name;
    document.getElementById('base-pages').textContent = d.pages;
    document.getElementById('base-info').style.display = 'block';
  }
});

// ── launch ────────────────────────────────────────────────────────────────────
let _t0=null,_ti=null;
async function launch(){
  const sel=[...document.querySelectorAll('input[type=checkbox]:checked:not(:disabled)')]
    .map(c=>c.value).filter(v=>!v.startsWith('phase')||v==='phase0'||v==='phase1'||
      document.querySelector(`[data-id="${v}"]`)?.dataset.parent===undefined);
  // Include all checked (let backend figure out which args to use)
  const all=[...document.querySelectorAll('input[type=checkbox]:checked:not(:disabled)')].map(c=>c.value);
  if(!all.length){alert('Sélectionnez au moins un groupe.');return;}
  const btn=document.getElementById('launch');btn.disabled=true;
  const stopBtn=document.getElementById('stop-btn');stopBtn.style.display='inline-block';stopBtn.disabled=false;
  const out=document.getElementById('out');
  out.innerHTML='';out.style.display='block';
  document.getElementById('idle').style.display='none';
  document.getElementById('status-bar').style.display='flex';
  document.getElementById('report-link').style.display='none';
  document.getElementById('dl-link').style.display='none';
  setBadge('running','⏳ En cours…');
  _t0=Date.now();_ti=setInterval(()=>{
    document.getElementById('elapsed').textContent=Math.round((Date.now()-_t0)/1000)+'s';
  },1000);
  const resp=await fetch('/run',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({groups:all})});
  if(!resp.ok){const e=await resp.json().catch(()=>({}));alert(e.error||'Erreur');btn.disabled=false;clearInterval(_ti);return;}
  const{report}=await resp.json();
  const es=new EventSource('/stream');
  es.onmessage=e=>{const l=JSON.parse(e.data);out.insertAdjacentHTML('beforeend',colorLine(l)+'\\n');out.scrollTop=out.scrollHeight};
  es.addEventListener('done',e=>{
    es.close();clearInterval(_ti);btn.disabled=false;stopBtn.style.display='none';
    const{status,returncode}=JSON.parse(e.data);
    document.getElementById('elapsed').textContent=Math.round((Date.now()-_t0)/1000)+'s';
    setBadge(status==='PASSED'?'passed':'failed',
             status==='PASSED'?'✅ Tous les tests ont passé':'❌ Échec (code '+returncode+')');
    if(report){
      const rl=document.getElementById('report-link');
      rl.href='/report/'+encodeURIComponent(report);rl.style.display='inline';
    }
    document.getElementById('dl-link').style.display='inline';
  });
  es.onerror=()=>{es.close();clearInterval(_ti);btn.disabled=false;stopBtn.style.display='none';setBadge('failed','⚠ Connexion perdue')};
}
async function stopTests(){
  if(!confirm("Arr\u00eater les tests en cours ?")) return;
  const r=await fetch("/stop",{method:"POST"});
  if(!r.ok){const e=await r.json().catch(()=>({}));alert(e.error||"Erreur");return;}
  document.getElementById("stop-btn").disabled=true;
  setBadge("stopped","\u23f9 Arr\u00eat demand\u00e9\u2026");
}
function setBadge(cls,txt){
  const b=document.getElementById('status-badge');b.className='badge-'+cls;b.textContent=txt;
}
</script>
</body>
</html>"""


@app.get("/")
def index():
    return render_template_string(_HTML, groups=GROUPS, server=SERVER)


@app.get("/healthz-proxy")
def healthz_proxy():
    import urllib.request
    try:
        urllib.request.urlopen(f"{SERVER}/healthz", timeout=3)
        return jsonify({"ok": True})
    except Exception:
        return jsonify({"ok": False}), 503


@app.post("/run")
def run_tests():
    global _job
    data     = request.json or {}
    selected = data.get("groups", [])
    with _lock:
        if _job and _job.get("running"):
            return jsonify({"error": "Tests déjà en cours"}), 409
        cmd  = ["python", "-m", "pytest", "-v", "--tb=short", "--no-header"]
        seen: list[str] = []
        for gid in selected:
            grp = next((g for g in GROUPS if g["id"] == gid and g["available"]), None)
            if grp and grp.get("args"):
                for a in grp["args"]:
                    if a not in seen:
                        seen.append(a)
        cmd.extend(seen)
        ts     = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        report = f"report/report_{ts}.html"
        Path("report").mkdir(exist_ok=True)
        cmd += ["--html", report, "--self-contained-html"]
        _job = {"running": True, "lines": [], "returncode": None,
                "report": report, "started_at": ts, "proc": None}
        def _run():
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            with _lock: _job["proc"] = proc
            for line in proc.stdout:
                with _lock: _job["lines"].append(line.rstrip())
            proc.wait()
            with _lock:
                _job["running"] = False
                _job["returncode"] = proc.returncode
                _job["proc"] = None
        threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "report": report})


@app.post("/stop")
def stop_tests():
    """Terminate the running pytest subprocess."""
    with _lock:
        if not _job or not _job.get("running"):
            return jsonify({"error": "Aucun test en cours"}), 409
        proc = _job.get("proc")
    if proc:
        proc.terminate()
    return jsonify({"ok": True})


@app.get("/stream")
def stream():
    def _gen():
        sent = 0
        while True:
            with _lock:
                if _job is None: yield "data: {}\n\n"; return
                lines = _job["lines"][:]; running = _job["running"]; rc = _job["returncode"]
            for line in lines[sent:]:
                yield f"data: {json.dumps(line)}\n\n"; sent += 1
            if not running:
                s = "PASSED" if rc == 0 else "FAILED"
                yield f"event: done\ndata: {json.dumps({'status':s,'returncode':rc})}\n\n"; return
            time.sleep(0.15)
    return Response(_gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/report/<path:filename>")
def serve_report(filename: str):
    p = Path(filename)
    if not p.exists() or p.suffix != ".html": return "Rapport introuvable", 404
    return p.read_text(encoding="utf-8"), 200, {"Content-Type": "text/html; charset=utf-8"}


@app.post("/upload-base")
def upload_base():
    f = request.files.get("file")
    if not f or not (f.filename or "").lower().endswith(".pdf"):
        return jsonify({"error": "Fichier PDF requis"}), 400
    data = f.read()
    try:
        from pypdf import PdfReader
        pages = len(PdfReader(io.BytesIO(data)).pages)
    except Exception:
        return jsonify({"error": "PDF invalide ou corrompu"}), 400
    BASE_PDF.write_bytes(data)
    meta = {"name": secure_filename(f.filename), "pages": pages}
    BASE_META.write_text(json.dumps(meta))
    return jsonify({"ok": True, **meta})


@app.delete("/upload-base")
def delete_base():
    BASE_PDF.unlink(missing_ok=True)
    BASE_META.unlink(missing_ok=True)
    return jsonify({"ok": True})


@app.get("/base-info")
def base_info():
    if BASE_META.exists():
        return jsonify(json.loads(BASE_META.read_text()))
    return jsonify({"name": None, "pages": 0})


@app.get("/download")
def download():
    """Download the latest test session outputs as a ZIP."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Latest log directory
        log_base = Path("logs")
        if log_base.exists():
            runs = sorted(log_base.iterdir(), key=lambda p: p.name, reverse=True)
            if runs:
                latest = runs[0]
                for f in latest.iterdir():
                    if f.is_file():
                        zf.write(f, f"logs/{latest.name}/{f.name}")
        # Current job's HTML report
        with _lock:
            report = _job.get("report") if _job else None
        if report:
            p = Path(report)
            if p.exists():
                zf.write(p, p.name)
    buf.seek(0)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return send_file(
        buf, mimetype="application/zip", as_attachment=True,
        download_name=f"tester_output_{ts}.zip"
    )


@app.get("/status")
def status():
    with _lock:
        if _job is None: return jsonify({"status": "idle"})
        return jsonify({"status": "running" if _job["running"] else
                        ("passed" if _job["returncode"] == 0 else "failed"),
                        "returncode": _job["returncode"], "lines": len(_job["lines"]),
                        "report": _job.get("report")})


if __name__ == "__main__":
    print(f"pdf-dispatch-tester web UI → http://0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
