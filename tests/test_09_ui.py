"""
tests/test_09_ui.py — Phase 9 : Tests de l'interface utilisateur (Playwright).

Structure :

  TestUiSmoke   (Phase 9a) — Infrastructure : page charge, JS exécute,
                             i18n appliquée, stats numériques.
  TestUiTriggers            (Phase 9c — à venir)
  TestUiSeparator           (Phase 9d — à venir)
  TestUiOptions             (Phase 9e — à venir)
  TestUiEmailPanel          (Phase 9f — à venir)
  TestUiWebhook             (Phase 9g — à venir)

Marqueur : @pytest.mark.ui
  Tous les tests nécessitent Playwright + Chromium dans le container.
  Pour exclure : pytest -m "not ui"
  Pour cibler : pytest -m ui tests/test_09_ui.py
"""
from __future__ import annotations

import re

import pytest

from conftest import reload_and_wait, wait_for_refresh


# ─────────────────────────────────────────────────────────────────────────────
# Phase 9a — Smoke : infrastructure Playwright
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.ui
class TestUiSmoke:
    """
    Vérifie que le browser charge l'UI pdf-dispatch correctement.

    Ces tests ne touchent pas à la configuration — ils vérifient seulement
    que la page est fonctionnelle après le démarrage du container.
    """

    def test_page_loads(self, ui_page):
        """La page s'est chargée : la zone de dépôt principale est visible."""
        assert ui_page.locator("#upload-zone").count() == 1, (
            "La zone de dépôt (#upload-zone) est absente du DOM"
        )

    def test_no_uncaught_js_errors(self, ui_page):
        """Aucune exception JavaScript non gérée pendant le chargement."""
        assert ui_page._js_errors == [], (
            "Erreurs JS détectées lors du chargement :\n"
            + "\n".join(ui_page._js_errors)
        )

    def test_stats_are_numeric(self, ui_page):
        """Les compteurs de stats (#st-proc, #st-docs, #st-err) affichent des entiers."""
        for stat_id in ("st-proc", "st-docs", "st-err"):
            text = ui_page.locator(f"#{stat_id}").inner_text().strip()
            assert text.isdigit(), (
                f"#{stat_id} contient '{text}' — attendu un entier"
            )

    def test_no_raw_i18n_keys(self, ui_page):
        """
        Aucune clé i18n brute visible dans le texte de la page.

        Vérifie les clés connues pour avoir causé des régressions.
        Format d'une clé brute : 'section.sous_cle' (avec un point, tout en minuscules).
        """
        visible = ui_page.locator("body").inner_text()
        # Clés connues pour avoir été exposées brutes lors de régressions passées
        suspicious = [
            "common.none_no_code",
            "email.action_read",
            "email.action_delete",
            "email.use_ssl",
            "email.verify_ssl",
            "email.enabled",
            "triggers.placeholder",
            "header.status_idle",
            "header.status_processing",
        ]
        found = [k for k in suspicious if k in visible]
        assert not found, (
            f"Clés i18n brutes visibles dans la page : {found}\n"
            "→ applyI18n() n'a pas été appelé ou le fichier de traduction manque."
        )

    def test_log_section_present(self, ui_page):
        """Le journal d'activité (#log-wrap) est monté dans le DOM."""
        assert ui_page.locator("#log-wrap").is_visible()

    def test_app_version_displayed(self, ui_page):
        """L'élément version de l'application est présent (peut être vide en test)."""
        el = ui_page.locator("#app-version")
        assert el.count() == 1, "#app-version absent du DOM"

    def test_queue_status_idle(self, ui_page):
        """
        À froid (aucun fichier en cours), le statut affiche l'état idle.

        Vérifie aussi que le dot de statut (#sdot) et le texte (#stext)
        sont présents — régression possible si refresh() ne s'exécute pas.
        """
        assert ui_page.locator("#sdot").count() == 1
        assert ui_page.locator("#stext").count() == 1
        # Le texte idle dépend de la langue ; on vérifie juste qu'il n'est pas vide
        status_text = ui_page.locator("#stext").inner_text().strip()
        assert status_text != "", "#stext est vide — refresh() n'a peut-être pas abouti"


# ─────────────────────────────────────────────────────────────────────────────
# Phases 9c–9h — à implémenter
# ─────────────────────────────────────────────────────────────────────────────

# @pytest.mark.ui
# class TestUiTriggers: ...
#
# @pytest.mark.ui
# class TestUiSeparator: ...
#
# @pytest.mark.ui
# class TestUiOptions: ...
#
# @pytest.mark.ui
# class TestUiEmailPanel: ...
#
# @pytest.mark.ui
# class TestUiWebhook: ...
