"""
instance_check.py — which pdf-dispatch instance is the tester really talking to?

The tester shares the network namespace of the pdf-dispatch-test container
(network_mode: service:), so "localhost:5000" is whatever container the tester
was attached to when IT was created. If only pdf-dispatch is recreated (new
image), the tester keeps talking to the OLD instance, which Docker cannot
remove while the tester still uses its network — and every test runs against
the old code without any visible sign.

check_instance() compares:
  * the version answered on TESTER_SERVER (what the tests will hit), with
  * the version answered by every address the service name resolves to
    (TESTER_PEER_HOST, default "pdf-dispatch-test": the current container,
    plus any orphan still holding the alias), and
  * optionally TESTER_EXPECT_VERSION (prefix, e.g. "v1.8.1-3").

Statuses:
  ok          same version everywhere (and expected prefix matches, if set)
  mismatch    another instance answers a different version   → blocking
  unexpected  tested version does not start with the expected → blocking
  unknown     service name not resolvable or not answering (e.g. run outside
              the compose stack): only the tested version is shown → not blocking
  down        TESTER_SERVER does not answer /api/runtime       → blocking
"""

from __future__ import annotations

import json
import os
import socket
import urllib.request
from urllib.parse import urlparse

BLOCKING = {"mismatch", "unexpected", "down"}


def _fetch_version(base_url: str, api_key: str, timeout: float = 4) -> str:
    req = urllib.request.Request(base_url.rstrip("/") + "/api/runtime",
                                 headers={"X-API-Key": api_key,
                                          "Accept": "application/json"})
    # Direct connection: an environment proxy must never answer for the
    # instance under test.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8")).get("app_version") or "?"


def _resolve(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    seen: list[str] = []
    for *_, sockaddr in infos:
        if sockaddr[0] not in seen:
            seen.append(sockaddr[0])
    return seen


def check_instance(server: str | None = None, api_key: str | None = None,
                   peer_host: str | None = None, expect: str | None = None,
                   fetch=_fetch_version, resolve=_resolve) -> dict:
    server = (server or os.environ.get("TESTER_SERVER", "http://localhost:5000")).rstrip("/")
    api_key = api_key if api_key is not None else os.environ.get("TESTER_API_KEY", "")
    peer_host = peer_host if peer_host is not None else os.environ.get(
        "TESTER_PEER_HOST", "pdf-dispatch-test")
    expect = (expect if expect is not None else os.environ.get("TESTER_EXPECT_VERSION", "")).strip()

    result = {"status": "ok", "tested": None, "peers": {}, "expected": expect or None,
              "peer_host": peer_host, "message": ""}
    try:
        result["tested"] = fetch(server, api_key)
    except Exception as exc:
        result.update(status="down",
                      message=f"pdf-dispatch ne répond pas sur {server} ({exc}).")
        return result
    tested = result["tested"]

    port = urlparse(server).port or 80
    try:
        addresses = resolve(peer_host, port) if peer_host else []
    except Exception:
        addresses = []
    answered = {}
    for ip in addresses:
        host = f"[{ip}]" if ":" in ip else ip
        try:
            answered[ip] = result["peers"][ip] = fetch(f"http://{host}:{port}", api_key)
        except Exception as exc:
            result["peers"][ip] = f"injoignable ({type(exc).__name__})"

    # Only versions actually answered count: an unreachable address is not
    # evidence of another version.
    others = sorted({v for v in answered.values() if v != tested})
    if others:
        result.update(status="mismatch", message=(
            f"Plusieurs instances de pdf-dispatch coexistent : le tester parle à {tested}, "
            f"mais « {peer_host} » répond aussi {', '.join(others)}. Soit le tester est resté "
            "attaché à l'ancienne instance, soit un ancien conteneur tourne encore. "
            "Arrêtez TOUTE la stack de test, supprimez tout conteneur pdf-dispatch orphelin, "
            "puis redémarrez la stack."))
    elif expect and not tested.startswith(expect):
        result.update(status="unexpected", message=(
            f"Version testée {tested}, attendue {expect}* (TESTER_EXPECT_VERSION). "
            "Re-téléchargez l'image et redéployez toute la stack, ou corrigez la valeur attendue."))
    elif not answered:
        result.update(status="unknown", message=(
            f"« {peer_host} » introuvable ou injoignable : impossible de vérifier qu'il "
            "n'existe pas d'autre instance (normal hors de la stack compose)."))
    return result


def summary_line(result: dict) -> str:
    icon = {"ok": "✅", "unknown": "⚠️"}.get(result["status"], "❌")
    line = f"{icon} pdf-dispatch testé : {result['tested'] or '?'}"
    if result["status"] != "ok":
        line += f" — {result['message']}"
    return line
