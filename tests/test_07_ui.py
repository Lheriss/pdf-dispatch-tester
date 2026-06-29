"""
tests/test_07_ui.py — Phase 7 : Tests de l'interface utilisateur (Playwright).

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
  Pour cibler : pytest -m ui tests/test_07_ui.py
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
# Helpers partagés entre les classes UI (fonctions de module, non-fixtures)
# ─────────────────────────────────────────────────────────────────────────────

def _open_settings_section(page) -> None:
    """Ouvre #sbody si pas encore ouvert et attend que loadConfig() ait peuplé cfg."""
    if "open" not in (page.locator("#sbody").get_attribute("class") or ""):
        page.locator(".settings-header").click()
    page.wait_for_function("() => typeof cfg !== 'undefined' && !!(cfg && cfg.loaded)")


def _open_options_section(page) -> None:
    """Ouvre #options-body (nécessite que #sbody soit déjà ouvert)."""
    body = page.locator("#options-body")
    if not body.is_visible():
        page.locator(".osection-header").click()
        body.wait_for(state="visible")


def _open_email_section(page) -> None:
    """Ouvre #email-section.

    #email-panel-btn est dans #options-body (sous-section de #sbody).
    Il faut donc ouvrir #options-body avant de cliquer le bouton.
    Prérequis : #sbody doit déjà être ouvert (_open_settings_section).
    """
    _open_options_section(page)   # #email-panel-btn vit dans #options-body
    section = page.locator("#email-section")
    if not section.is_visible():
        page.locator("#email-panel-btn").click()
        section.wait_for(state="visible")


def _create_and_open_email_draft(page, name: str = "") -> None:
    """Crée un brouillon de config email et ouvre son panneau de formulaire."""
    page.locator("#new-email-config-name").fill(name)
    page.locator("button[onclick='addEmailConfig()']").click()
    tag = page.locator("#email-config-list .trigger-tag").first
    tag.wait_for(state="visible")
    if not page.locator("#email-config-panel.open").is_visible():
        tag.click()
    page.locator("#email-config-panel.open").wait_for(state="attached", timeout=8_000)
    wait_for_refresh(page)  # laisser refresh() peupler cfg.split_values (FK3) et syncer les radios


# ─────────────────────────────────────────────────────────────────────────────
# Phase 9c — Triggers CRUD via l'UI
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.ui
class TestUiTriggers:
    """Phase 7c — Création, configuration et suppression de déclencheurs via l'UI.

    Chaque test part d'un état vierge (split_values=[]) imposé par l'API,
    puis interagit exclusivement via le navigateur pour vérifier que les
    actions UI produisent bien les effets attendus.
    """

    @pytest.fixture(autouse=True)
    def clean_triggers(self, http, server):
        """Assure split_values=[] avant et après chaque test."""
        from helpers import set_config
        set_config(http, server, split_values=[])
        yield
        set_config(http, server, split_values=[])

    def test_settings_section_toggles(self, ui_page):
        """Cliquer sur l'en-tête Settings ouvre #sbody (classe 'open'), re-cliquer ferme."""
        sbody = ui_page.locator("#sbody")
        assert "open" not in (sbody.get_attribute("class") or ""), \
            "#sbody ne devrait pas être ouvert au chargement"
        ui_page.locator(".settings-header").click()
        ui_page.wait_for_function(
            "() => document.getElementById('sbody').classList.contains('open')"
        )
        assert "open" in sbody.get_attribute("class")
        # Toggle retour
        ui_page.locator(".settings-header").click()
        ui_page.wait_for_function(
            "() => !document.getElementById('sbody').classList.contains('open')"
        )
        assert "open" not in sbody.get_attribute("class")

    def test_add_trigger_via_enter(self, ui_page):
        """Saisir une valeur dans #new-trigger + Entrée → tag apparaît dans #trigger-list."""
        _open_settings_section(ui_page)
        ui_page.locator("#new-trigger").fill("TESTTRIG")
        ui_page.locator("#new-trigger").press("Enter")
        ui_page.locator("#trigger-list .trigger-tag").wait_for(state="visible")
        assert "TESTTRIG" in ui_page.locator("#trigger-list").inner_text()

    def test_add_trigger_via_button(self, ui_page):
        """Cliquer sur le bouton Ajouter crée aussi un tag déclencheur."""
        _open_settings_section(ui_page)
        ui_page.locator("#new-trigger").fill("BTNTRIG")
        ui_page.locator("button[onclick='addTrigger()']").click()
        ui_page.locator("#trigger-list .trigger-tag").wait_for(state="visible")
        assert "BTNTRIG" in ui_page.locator("#trigger-list").inner_text()

    def test_trigger_panel_opens_on_click(self, ui_page):
        """Cliquer sur un tag ouvre #trigger-panel avec la valeur dans #tp-title."""
        _open_settings_section(ui_page)
        ui_page.locator("#new-trigger").fill("FK3")
        ui_page.locator("#new-trigger").press("Enter")
        ui_page.locator("#trigger-list .trigger-tag").wait_for(state="visible")
        ui_page.locator("#trigger-list .trigger-tag").first.click()
        ui_page.wait_for_function(
            "() => document.getElementById('trigger-panel').classList.contains('open')"
        )
        assert ui_page.locator("#tp-title").inner_text().strip() == "FK3"

    def test_delete_page_toggle_adds_scissors_icon(self, ui_page):
        """Cocher #tp-delete-page fait apparaître l'icône ✂ sur le tag."""
        _open_settings_section(ui_page)
        ui_page.locator("#new-trigger").fill("DELTRIG")
        ui_page.locator("#new-trigger").press("Enter")
        ui_page.locator("#trigger-list .trigger-tag").first.click()
        ui_page.wait_for_function(
            "() => document.getElementById('trigger-panel').classList.contains('open')"
        )
        ui_page.locator("label.toggle:has(#tp-delete-page)").click()  # clic sur le label visible du CSS toggle
        ui_page.wait_for_function(
            "() => document.querySelector('#trigger-list .del-icon') !== null"
        )

    def test_case_insensitive_toggle_adds_aa_icon(self, ui_page):
        """Décocher #tp-case-sensitive fait apparaître l'icône Aa sur le tag."""
        _open_settings_section(ui_page)
        ui_page.locator("#new-trigger").fill("CASETRIG")
        ui_page.locator("#new-trigger").press("Enter")
        ui_page.locator("#trigger-list .trigger-tag").first.click()
        ui_page.wait_for_function(
            "() => document.getElementById('trigger-panel').classList.contains('open')"
        )
        ui_page.locator("label.toggle:has(#tp-case-sensitive)").click()  # clic sur le label visible du CSS toggle
        ui_page.wait_for_function(
            "() => document.querySelector('#trigger-list .case-icon') !== null"
        )

    def test_remove_trigger(self, ui_page):
        """Cliquer sur ✕ supprime le tag déclencheur de la liste."""
        _open_settings_section(ui_page)
        ui_page.locator("#new-trigger").fill("RMTRIG")
        ui_page.locator("#new-trigger").press("Enter")
        tag = ui_page.locator("#trigger-list .trigger-tag").first
        tag.wait_for(state="visible")
        tag.locator(".rm").click()
        ui_page.wait_for_function(
            "() => !document.querySelector('#trigger-list .trigger-tag')"
        )
        assert "RMTRIG" not in ui_page.locator("#trigger-list").inner_text()

    def test_trigger_persists_after_reload(self, ui_page):
        """Un déclencheur créé via l'UI survit à un rechargement de page."""
        _open_settings_section(ui_page)
        ui_page.locator("#new-trigger").fill("PERSISTTRIG")
        ui_page.locator("#new-trigger").press("Enter")
        ui_page.locator("#trigger-list .trigger-tag").wait_for(state="visible")
        reload_and_wait(ui_page)
        _open_settings_section(ui_page)
        assert "PERSISTTRIG" in ui_page.locator("#trigger-list").inner_text()

    def test_duplicate_trigger_not_added(self, ui_page):
        """Ajouter deux fois la même valeur ne crée qu'un seul tag."""
        _open_settings_section(ui_page)
        ui_page.locator("#new-trigger").fill("DUP")
        ui_page.locator("#new-trigger").press("Enter")
        ui_page.locator("#trigger-list .trigger-tag").wait_for(state="visible")
        ui_page.locator("#new-trigger").fill("DUP")
        ui_page.locator("#new-trigger").press("Enter")
        count = ui_page.locator("#trigger-list .trigger-tag").count()
        assert count == 1, f"Doublon créé — attendu 1 tag, obtenu {count}"


# ─────────────────────────────────────────────────────────────────────────────
# Phase 9e — Options
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.ui
class TestUiOptions:
    """Phase 7e — Panneau Options : placement du séparateur et toggles.

    Vérifie que les contrôles du panneau Options reflètent la configuration
    serveur et que les changements persistent après rechargement.
    """

    @pytest.fixture(autouse=True)
    def reset_options(self, http, server):
        """Remet les options à un état connu avant/après chaque test."""
        from helpers import set_config
        set_config(http, server,
                   separator_placement="before",
                   delete_source=False,
                   subdirs_by_trigger=False,
                   log_verbose=False)
        yield
        set_config(http, server,
                   separator_placement="before",
                   delete_source=False,
                   subdirs_by_trigger=False,
                   log_verbose=False)

    def test_options_section_opens(self, ui_page):
        """Cliquer sur .osection-header rend #options-body visible."""
        _open_settings_section(ui_page)
        body = ui_page.locator("#options-body")
        assert not body.is_visible(), "#options-body devrait être caché par défaut"
        _open_options_section(ui_page)
        assert body.is_visible()

    def test_separator_before_is_default(self, ui_page):
        """Après reset, opt-sep-before est coché et opt-sep-after ne l'est pas."""
        _open_settings_section(ui_page)
        _open_options_section(ui_page)
        assert ui_page.locator("#opt-sep-before").is_checked(), \
            "#opt-sep-before devrait être coché par défaut"
        assert not ui_page.locator("#opt-sep-after").is_checked()

    def test_separator_after_persists(self, ui_page):
        """Sélectionner 'after' et recharger maintient 'after' sélectionné."""
        _open_settings_section(ui_page)
        _open_options_section(ui_page)
        ui_page.locator("#opt-sep-after").click()
        wait_for_refresh(ui_page)
        reload_and_wait(ui_page)
        _open_settings_section(ui_page)
        _open_options_section(ui_page)
        assert ui_page.locator("#opt-sep-after").is_checked(), \
            "#opt-sep-after devrait persister après rechargement"

    def test_delete_source_toggle_persists(self, ui_page):
        """Activer #opt-delete persiste après rechargement."""
        _open_settings_section(ui_page)
        _open_options_section(ui_page)
        assert not ui_page.locator("#opt-delete").is_checked()
        ui_page.locator("label.toggle:has(#opt-delete)").click()  # clic sur le label visible du CSS toggle
        wait_for_refresh(ui_page)
        reload_and_wait(ui_page)
        _open_settings_section(ui_page)
        _open_options_section(ui_page)
        assert ui_page.locator("#opt-delete").is_checked(), \
            "#opt-delete devrait être coché après rechargement"


# ─────────────────────────────────────────────────────────────────────────────
# Phase 9f — Panneau email : régressions CSS/UI connues
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.ui
class TestUiEmailPanel:
    """Phase 7f — Panneau de configuration email : régressions CSS/UI.

    Ces tests ciblent les bugs UI spécifiquement documentés :
    - Clés i18n brutes visibles sur les labels action (email.action_read…)
    - Toggle SSL/TLS ne grisait pas la ligne Vérifier SSL (Safari :checked)
    - Dropdown déclencheur non peuplé (renderEmailTriggerSelect vide)
    - Radio buttons action non rendus / non sélectionnables
    """

    @pytest.fixture(autouse=True)
    def setup_email_panel(self, http, server):
        """Prépare un déclencheur FK3 (pour le dropdown) et nettoie les configs email."""
        from helpers import set_config
        set_config(http, server,
                   split_values=[{"value": "FK3", "page_handling": "keep"}])
        r = http.get(f"{server}/api/state")
        for ec in r.json().get("app_config", {}).get("email_configs", []):
            http.delete(f"{server}/api/email/configs/{ec['id']}")
        yield
        r = http.get(f"{server}/api/state")
        for ec in r.json().get("app_config", {}).get("email_configs", []):
            http.delete(f"{server}/api/email/configs/{ec['id']}")
        set_config(http, server, split_values=[])

    def test_email_section_opens(self, ui_page):
        """Cliquer #email-panel-btn (dans #sbody) rend #email-section visible."""
        _open_settings_section(ui_page)
        section = ui_page.locator("#email-section")
        assert not section.is_visible()
        _open_email_section(ui_page)
        assert section.is_visible()

    def test_action_radio_buttons_all_present(self, ui_page):
        """Les 3 radio buttons action (read/delete/ignore) sont présents et visibles.

        Régression : les radios n'apparaissaient pas suite à un bug de rendu.
        """
        _open_settings_section(ui_page)
        _open_email_section(ui_page)
        _create_and_open_email_draft(ui_page, "test-radios")
        for value in ("read", "delete", "ignore"):
            radio = ui_page.locator(f"input[name='em-action'][value='{value}']")
            assert radio.count() == 1, f"Radio action='{value}' absent du DOM"
            assert radio.is_visible(), f"Radio action='{value}' non visible"

    def test_action_radio_labels_translated(self, ui_page):
        """Les labels des radios action affichent du texte traduit, pas les clés i18n brutes.

        Régression : les clés 'email.action_read', 'email.action_delete',
        'email.action_ignore' apparaissaient telles quelles dans l'UI.
        """
        _open_settings_section(ui_page)
        _open_email_section(ui_page)
        _create_and_open_email_draft(ui_page, "test-i18n")
        panel_text = ui_page.locator("#email-config-panel").inner_text()
        for raw_key in ("email.action_read", "email.action_delete", "email.action_ignore"):
            assert raw_key not in panel_text, (
                f"Clé i18n brute '{raw_key}' visible dans le panneau email "
                "— régression applyI18n()"
            )

    def test_action_radio_selectable(self, ui_page):
        """Cliquer sur le radio 'delete' le coche et décoche 'read'."""
        _open_settings_section(ui_page)
        _open_email_section(ui_page)
        _create_and_open_email_draft(ui_page, "test-select")
        delete_radio = ui_page.locator("input[name='em-action'][value='delete']")
        read_radio   = ui_page.locator("input[name='em-action'][value='read']")
        assert read_radio.is_checked(), "Radio 'read' devrait être coché par défaut"
        delete_radio.click()
        assert delete_radio.is_checked(), "Radio 'delete' non coché après clic"
        assert not read_radio.is_checked(), "Radio 'read' devrait être décoché"

    def test_ssl_toggle_dims_verify_row(self, ui_page):
        """Décocher 'Use SSL' grise et désactive la ligne 'Verify SSL'.

        Régression Safari : _emailUpdateSslRow() devait ajouter la classe .toggle-off
        en plus du style opacity car Safari ne repeignait pas :checked.
        """
        _open_settings_section(ui_page)
        _open_email_section(ui_page)
        _create_and_open_email_draft(ui_page, "test-ssl")
        use_ssl    = ui_page.locator("#em-use-ssl")
        verify_row = ui_page.locator("#em-ssl-row")
        ssl_input  = ui_page.locator("#em-ssl")
        assert use_ssl.is_checked(), "use_ssl devrait être coché par défaut (port 993)"
        initial_opacity = verify_row.evaluate(
            "el => parseFloat(el.style.opacity || '1')"
        )
        assert initial_opacity >= 0.9, f"Opacity initiale trop faible: {initial_opacity}"
        ui_page.evaluate(
            "const el = document.getElementById('em-use-ssl');"
            " el.checked = false;"
            " el.dispatchEvent(new Event('change', {bubbles:true}));"
        )  # set checked=false + fire change — .click() cause double-toggle (bubble vers label)
        ui_page.wait_for_function(
            "() => parseFloat(document.getElementById('em-ssl-row').style.opacity || '1') < 0.6",
            timeout=5_000,
        )
        assert not ssl_input.is_checked(), "#em-ssl devrait être décoché quand use_ssl=false"
        assert "toggle-off" in (verify_row.locator(".toggle").get_attribute("class") or ""), \
            "Classe .toggle-off manquante sur le label interne — régression Safari :checked repaint"

    def test_ssl_verify_row_re_enabled(self, ui_page):
        """Re-cocher 'Use SSL' restaure la ligne Verify SSL à son état actif."""
        _open_settings_section(ui_page)
        _open_email_section(ui_page)
        _create_and_open_email_draft(ui_page, "test-ssl-restore")
        use_ssl   = ui_page.locator("#em-use-ssl")
        ssl_input = ui_page.locator("#em-ssl")
        ui_page.evaluate(
            "const el = document.getElementById('em-use-ssl');"
            " el.checked = false;"
            " el.dispatchEvent(new Event('change', {bubbles:true}));"
        )  # set checked=false + fire change — .click() cause double-toggle (bubble vers label)
        ui_page.wait_for_function(
            "() => parseFloat(document.getElementById('em-ssl-row').style.opacity || '1') < 0.6"
        )
        ui_page.evaluate(
            "const el = document.getElementById('em-use-ssl');"
            " el.checked = true;"
            " el.dispatchEvent(new Event('change', {bubbles:true}));"
        )  # set checked=true + fire change — .click() cause double-toggle (bubble vers label)
        ui_page.wait_for_function(
            "() => parseFloat(document.getElementById('em-ssl-row').style.opacity || '1') >= 0.9"
        )
        assert ssl_input.is_checked(), "#em-ssl devrait être re-coché quand use_ssl=true"

    def test_default_trigger_dropdown_populated(self, ui_page):
        """#em-default-trigger contient le déclencheur FK3 configuré.

        Régression : renderEmailTriggerSelect() lisait cfg.split_values avant
        que cfg soit chargé → dropdown vide sauf l'option 'aucun'.
        """
        _open_settings_section(ui_page)
        _open_email_section(ui_page)
        _create_and_open_email_draft(ui_page, "test-dropdown")
        dropdown = ui_page.locator("#em-default-trigger")
        options_count = dropdown.locator("option").count()
        assert options_count >= 2, (
            f"Dropdown #em-default-trigger : attendu ≥2 options (none + FK3), "
            f"obtenu {options_count} — régression renderEmailTriggerSelect()"
        )
        assert "FK3" in dropdown.inner_text(), \
            f"Trigger 'FK3' absent du dropdown"

    def test_port_auto_ssl_toggle(self, ui_page):
        """Changer le port vers 143 décoche 'Use SSL' ; 993 le recoche."""
        _open_settings_section(ui_page)
        _open_email_section(ui_page)
        _create_and_open_email_draft(ui_page, "test-port-ssl")
        use_ssl    = ui_page.locator("#em-use-ssl")
        port_input = ui_page.locator("#em-port")
        assert use_ssl.is_checked()
        port_input.fill("143")  # fill() efface et remplace
        ui_page.evaluate("emailAutoSsl()")  # appel direct : dispatch_event ne déclenche pas oninput en headless
        ui_page.wait_for_function(
            "() => !document.getElementById('em-use-ssl').checked",
            timeout=3_000,
        )
        assert not use_ssl.is_checked()
        port_input.fill("993")  # fill() efface et remplace
        ui_page.evaluate("emailAutoSsl()")  # appel direct : dispatch_event ne déclenche pas oninput en headless
        ui_page.wait_for_function(
            "() => document.getElementById('em-use-ssl').checked",
            timeout=3_000,
        )
        assert use_ssl.is_checked()

# ─────────────────────────────────────────────────────────────────────────────
# Helper — panneau Webhook
# ─────────────────────────────────────────────────────────────────────────────

def _open_webhook_section(page) -> None:
    """Ouvre #webhook-panel.

    #webhook-panel-btn est dans #options-body (meme niveau que #email-panel-btn).
    Prerequis: #sbody ouvert via _open_settings_section.
    """
    _open_options_section(page)
    panel = page.locator("#webhook-panel")
    if not panel.is_visible():
        page.locator("#webhook-panel-btn").click()
        panel.wait_for(state="visible")


# ─────────────────────────────────────────────────────────────────────────────
# Phase 7d — Separateur : labels et exclusion mutuelle
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.ui
class TestUiSeparator:
    """Phase 7d — Section separator placement dans Options.

    TestUiOptions verifie la persistance. Cette classe verifie
    les proprietes structurelles : radios presents, labels traduits,
    exclusion mutuelle du groupe radio.
    """

    @pytest.fixture(autouse=True)
    def _reset_sep(self, http, server):
        from helpers import set_config
        set_config(http, server, separator_placement="before")
        yield
        set_config(http, server, separator_placement="before")

    def test_separator_heading_translated(self, ui_page):
        """Le titre Separator placement est affiche en texte traduit."""
        _open_settings_section(ui_page)
        _open_options_section(ui_page)
        el = ui_page.locator("[data-i18n='options.separator_placement']")
        assert el.is_visible()
        text = el.inner_text().strip()
        assert text and "options.separator_placement" not in text, (
            f"Cle i18n brute visible : {text!r}"
        )

    def test_both_separator_radios_present(self, ui_page):
        """#opt-sep-before et #opt-sep-after sont presents dans le DOM."""
        _open_settings_section(ui_page)
        _open_options_section(ui_page)
        assert ui_page.locator("#opt-sep-before").count() == 1
        assert ui_page.locator("#opt-sep-after").count() == 1

    def test_separator_before_default(self, ui_page):
        """Apres reset (placement=before), before est coche et after decoche."""
        _open_settings_section(ui_page)
        _open_options_section(ui_page)
        wait_for_refresh(ui_page)
        assert ui_page.locator("#opt-sep-before").is_checked()
        assert not ui_page.locator("#opt-sep-after").is_checked()

    def test_separator_radios_mutually_exclusive(self, ui_page):
        """Cliquer after decoche automatiquement before."""
        _open_settings_section(ui_page)
        _open_options_section(ui_page)
        wait_for_refresh(ui_page)
        assert ui_page.locator("#opt-sep-before").is_checked()
        ui_page.locator("label:has(#opt-sep-after)").click()
        wait_for_refresh(ui_page)
        assert ui_page.locator("#opt-sep-after").is_checked(), (
            "'after' doit etre coche apres clic"
        )
        assert not ui_page.locator("#opt-sep-before").is_checked(), (
            "'before' doit etre decoche — exclusion mutuelle"
        )

    def test_separator_radio_labels_translated(self, ui_page):
        """Les labels des radios affichent du texte traduit, pas des cles brutes."""
        _open_settings_section(ui_page)
        _open_options_section(ui_page)
        for key in ("options.separator_before", "options.separator_after"):
            els = ui_page.locator(f"[data-i18n='{key}']")
            if els.count() > 0:
                text = els.first.inner_text().strip()
                assert key not in text, f"Cle i18n brute : {text!r}"


# ─────────────────────────────────────────────────────────────────────────────
# Phase 7g — Webhook : configuration via l'UI
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.ui
class TestUiWebhook:
    """Phase 7g — Panneau Webhook HTTP.

    #webhook-panel-btn est dans #options-body (meme niveau que #email-panel-btn).
    _open_webhook_section() gere l'ouverture complete.
    """

    @pytest.fixture(autouse=True)
    def _reset_webhook(self, http, server):
        http.post(f"{server}/api/config", json={
            "webhook_enabled": False,
            "webhook_url":     "",
            "webhook_events":  "all",
        })
        yield
        http.post(f"{server}/api/config", json={
            "webhook_enabled": False,
            "webhook_url":     "",
            "webhook_events":  "all",
        })

    def test_webhook_panel_opens(self, ui_page):
        """#webhook-panel-btn ouvre #webhook-panel."""
        _open_settings_section(ui_page)
        _open_webhook_section(ui_page)
        assert ui_page.locator("#webhook-panel").is_visible()

    def test_webhook_initially_disabled(self, ui_page):
        """#opt-webhook-enabled est decoche par defaut."""
        _open_settings_section(ui_page)
        _open_webhook_section(ui_page)
        wait_for_refresh(ui_page)
        assert not ui_page.locator("#opt-webhook-enabled").is_checked()

    def test_webhook_config_hidden_when_disabled(self, ui_page):
        """#webhook-config est masque quand webhook desactive."""
        _open_settings_section(ui_page)
        _open_webhook_section(ui_page)
        wait_for_refresh(ui_page)
        assert not ui_page.locator("#webhook-config").is_visible()

    def test_webhook_enable_reveals_config(self, ui_page):
        """Activer le toggle rend #webhook-config visible."""
        _open_settings_section(ui_page)
        _open_webhook_section(ui_page)
        wait_for_refresh(ui_page)
        ui_page.locator("label.toggle:has(#opt-webhook-enabled)").click()
        ui_page.wait_for_function(
            "() => document.getElementById('webhook-config').style.display === 'block'",
            timeout=3_000,
        )
        assert ui_page.locator("#webhook-config").is_visible()

    def test_webhook_disable_hides_config(self, ui_page):
        """Desactiver le toggle masque a nouveau #webhook-config."""
        _open_settings_section(ui_page)
        _open_webhook_section(ui_page)
        wait_for_refresh(ui_page)
        ui_page.locator("label.toggle:has(#opt-webhook-enabled)").click()
        ui_page.wait_for_function(
            "() => document.getElementById('webhook-config').style.display === 'block'",
            timeout=3_000,
        )
        ui_page.locator("label.toggle:has(#opt-webhook-enabled)").click()
        ui_page.wait_for_function(
            "() => document.getElementById('webhook-config').style.display !== 'block'",
            timeout=3_000,
        )
        assert not ui_page.locator("#webhook-config").is_visible()

    def test_webhook_events_has_three_options(self, ui_page):
        """#opt-webhook-events contient exactement 3 options : all/success/error."""
        _open_settings_section(ui_page)
        _open_webhook_section(ui_page)
        wait_for_refresh(ui_page)
        ui_page.locator("label.toggle:has(#opt-webhook-enabled)").click()
        ui_page.locator("#webhook-config").wait_for(state="visible")
        opts = ui_page.locator("#opt-webhook-events option")
        assert opts.count() == 3, f"Attendu 3 options, obtenu {opts.count()}"
        values = {opts.nth(i).get_attribute("value") for i in range(3)}
        assert values == {"all", "success", "error"}, f"Options incorrectes : {values}"

    def test_webhook_events_default_is_all(self, ui_page):
        """Apres reset, #opt-webhook-events vaut 'all'."""
        _open_settings_section(ui_page)
        _open_webhook_section(ui_page)
        wait_for_refresh(ui_page)
        ui_page.locator("label.toggle:has(#opt-webhook-enabled)").click()
        ui_page.locator("#webhook-config").wait_for(state="visible")
        assert ui_page.locator("#opt-webhook-events").input_value() == "all"

    def test_webhook_url_field_accepts_input(self, ui_page):
        """Saisir une URL dans #opt-webhook-url la reflète immediatement."""
        _open_settings_section(ui_page)
        _open_webhook_section(ui_page)
        wait_for_refresh(ui_page)
        ui_page.locator("label.toggle:has(#opt-webhook-enabled)").click()
        ui_page.locator("#webhook-config").wait_for(state="visible")
        url = "https://hooks.example.com/test"
        ui_page.locator("#opt-webhook-url").fill(url)
        assert ui_page.locator("#opt-webhook-url").input_value() == url

    def test_webhook_enabled_persists_after_reload(self, ui_page):
        """Activer le webhook + rechargement : toujours actif."""
        _open_settings_section(ui_page)
        _open_webhook_section(ui_page)
        wait_for_refresh(ui_page)
        ui_page.locator("label.toggle:has(#opt-webhook-enabled)").click()
        ui_page.wait_for_function(
            "() => document.getElementById('webhook-config').style.display === 'block'",
            timeout=3_000,
        )
        wait_for_refresh(ui_page)
        reload_and_wait(ui_page)
        _open_settings_section(ui_page)
        _open_webhook_section(ui_page)
        wait_for_refresh(ui_page)
        assert ui_page.locator("#opt-webhook-enabled").is_checked(), (
            "Webhook doit rester active apres rechargement"
        )

    def test_webhook_url_persists_after_reload(self, ui_page):
        """URL saisie + rechargement : URL conservee."""
        _open_settings_section(ui_page)
        _open_webhook_section(ui_page)
        wait_for_refresh(ui_page)
        ui_page.locator("label.toggle:has(#opt-webhook-enabled)").click()
        ui_page.locator("#webhook-config").wait_for(state="visible")
        url = "https://hooks.example.com/persist"
        ui_page.locator("#opt-webhook-url").fill(url)
        ui_page.locator("#opt-webhook-url").dispatch_event("input")
        wait_for_refresh(ui_page)
        reload_and_wait(ui_page)
        _open_settings_section(ui_page)
        _open_webhook_section(ui_page)
        wait_for_refresh(ui_page)
        saved = ui_page.locator("#opt-webhook-url").input_value()
        assert saved == url, f"URL attendue {url!r}, obtenu {saved!r}"

# ─────────────────────────────────────────────────────────────────────────────
# Helpers — panneaux Dirs et ApiKey (dans #options-body)
# ─────────────────────────────────────────────────────────────────────────────

def _open_dirs_section(page) -> None:
    """Ouvre #dirs-panel (dans #options-body)."""
    _open_options_section(page)
    panel = page.locator("#dirs-panel")
    if not panel.is_visible():
        page.locator("#dirs-panel-btn").click()
        panel.wait_for(state="visible")


def _open_apikey_section(page) -> None:
    """Ouvre #apikey-panel (dans #options-body)."""
    _open_options_section(page)
    panel = page.locator("#apikey-panel")
    if not panel.is_visible():
        page.locator("#apikey-panel-btn").click()
        panel.wait_for(state="visible")


@pytest.mark.ui
class TestUiDirs:
    """Phase 7h — Panneau Configurer les dossiers."""

    def test_dirs_panel_opens(self, ui_page):
        """#dirs-panel-btn ouvre #dirs-panel."""
        _open_settings_section(ui_page)
        _open_dirs_section(ui_page)
        assert ui_page.locator("#dirs-panel").is_visible()

    def test_data_root_populated(self, ui_page):
        """#dir-data-root affiche un chemin (pas vide ni tiret)."""
        _open_settings_section(ui_page)
        _open_dirs_section(ui_page)
        wait_for_refresh(ui_page)
        root_text = ui_page.locator("#dir-data-root").inner_text().strip()
        assert root_text and root_text not in ("-", chr(8211), ""), (
            f"#dir-data-root must show a path, got {root_text!r}"
        )
        assert "/" in root_text, f"Path must contain /: {root_text!r}"

    def test_dirs_table_has_rows(self, ui_page):
        """#dirs-tbody contient au moins 3 lignes (input, output, error)."""
        _open_settings_section(ui_page)
        _open_dirs_section(ui_page)
        wait_for_refresh(ui_page)
        rows = ui_page.locator("#dirs-tbody tr")
        assert rows.count() >= 3, (
            f"Expected >= 3 rows in dirs table, got {rows.count()}"
        )

    def test_dirs_heading_translated(self, ui_page):
        """Titre du panneau traduit (pas cle i18n brute)."""
        _open_settings_section(ui_page)
        _open_dirs_section(ui_page)
        el = ui_page.locator("[data-i18n='dirs.panel_title']")
        if el.count() > 0:
            text = el.first.inner_text().strip()
            assert 'dirs.panel_title' not in text, f"i18n key visible: {text!r}"


@pytest.mark.ui
class TestUiApiKey:
    """Phase 7i — Panneau Acces API : visibilite et toggle."""

    def test_apikey_panel_opens(self, ui_page):
        """#apikey-panel-btn ouvre #apikey-panel."""
        _open_settings_section(ui_page)
        _open_apikey_section(ui_page)
        assert ui_page.locator("#apikey-panel").is_visible()

    def test_api_key_field_present_and_non_empty(self, ui_page):
        """#api-key-field est present et contient la cle."""
        _open_settings_section(ui_page)
        _open_apikey_section(ui_page)
        wait_for_refresh(ui_page)
        field = ui_page.locator("#api-key-field")
        assert field.count() == 1
        assert len(field.input_value()) >= 8, (
            f"API key must be non-empty, got {field.input_value()!r}"
        )

    def test_api_key_hidden_by_default(self, ui_page):
        """#api-key-field est de type password par defaut."""
        _open_settings_section(ui_page)
        _open_apikey_section(ui_page)
        wait_for_refresh(ui_page)
        t = ui_page.locator("#api-key-field").get_attribute("type")
        assert t == "password", f"Expected type=password, got {t!r}"

    def test_show_button_reveals_key(self, ui_page):
        """#api-key-show-btn change le type a text."""
        _open_settings_section(ui_page)
        _open_apikey_section(ui_page)
        wait_for_refresh(ui_page)
        ui_page.locator("#api-key-show-btn").click()
        t = ui_page.locator("#api-key-field").get_attribute("type")
        assert t == "text", f"After show click, expected text, got {t!r}"

    def test_show_button_rehides_key(self, ui_page):
        """Double clic revient a type=password."""
        _open_settings_section(ui_page)
        _open_apikey_section(ui_page)
        wait_for_refresh(ui_page)
        btn = ui_page.locator("#api-key-show-btn")
        btn.click(); btn.click()
        t = ui_page.locator("#api-key-field").get_attribute("type")
        assert t == "password", f"After re-hide, expected password, got {t!r}"

    def test_apikey_heading_translated(self, ui_page):
        """Titre traduit, pas de cle i18n brute."""
        _open_settings_section(ui_page)
        _open_apikey_section(ui_page)
        el = ui_page.locator("[data-i18n='apikey.section_title']")
        if el.count() > 0:
            assert 'apikey.section_title' not in el.first.inner_text()


@pytest.mark.ui
class TestUiFilenameTokens:
    """Phase 7j — Constructeur de tokens de nom de fichier.

    #token-list est dans #sbody (pas dans #options-body).
    """

    def test_token_list_in_dom_after_settings_open(self, ui_page):
        """#token-list est dans le DOM apres ouverture settings."""
        _open_settings_section(ui_page)
        wait_for_refresh(ui_page)
        assert ui_page.locator("#token-list").count() == 1

    def test_default_tokens_present(self, ui_page):
        """Au moins 2 tokens par defaut (trigger, counter)."""
        _open_settings_section(ui_page)
        wait_for_refresh(ui_page)
        items = ui_page.locator(
            "#token-list .token-item, #token-list [class*=tl]"
        )
        # token-list is populated via renderTokens() — give JS a moment
        ui_page.wait_for_function(
            "() => document.getElementById('token-list').children.length >= 1",
            timeout=5_000,
        )
        raw = ui_page.locator("#token-list").inner_html()
        assert raw.strip(), "token-list must not be empty after settings open"

    def test_add_string_token_button_present(self, ui_page):
        """Le bouton Ajouter un texte libre est visible."""
        _open_settings_section(ui_page)
        wait_for_refresh(ui_page)
        btn = ui_page.locator("button[onclick='addStringToken()']").first
        assert btn.count() > 0 or ui_page.locator(
            "[data-i18n='filename.add_free_text']"
        ).count() > 0, "addStringToken button must be present"

    def test_separator_buttons_present(self, ui_page):
        """Boutons separateurs (_ - .) presents dans le panneau."""
        _open_settings_section(ui_page)
        wait_for_refresh(ui_page)
        for sep_id in ("sep-_", "sep--", "sep-."):
            assert ui_page.locator(f'[id="{sep_id}"]').count() >= 1, (
                f"Separator button #{sep_id} must be present"
            )

    def test_add_string_token_increases_list(self, ui_page):
        """Cliquer Ajouter texte libre ajoute un token dans la liste."""
        _open_settings_section(ui_page)
        wait_for_refresh(ui_page)
        ui_page.wait_for_function(
            "() => document.getElementById('token-list').children.length >= 1",
            timeout=5_000,
        )
        before_count = ui_page.evaluate(
            "() => document.getElementById('token-list').children.length"
        )
        ui_page.locator("button[onclick='addStringToken()']").first.click()
        ui_page.wait_for_function(
            f"() => document.getElementById('token-list').children.length > {before_count}",
            timeout=3_000,
        )
        after_count = ui_page.evaluate(
            "() => document.getElementById('token-list').children.length"
        )
        assert after_count > before_count, (
            f"Token list must grow: {before_count} → {after_count}"
        )
