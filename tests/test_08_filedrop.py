"""
test_08_filedrop.py — Phase 8 : File-drop avancé.

Tests de robustesse du pipeline watchdog via dépôt direct de fichiers dans
/data/input/ et vérification double :
  – résultat de traitement via les événements /api/state
  – contenu du filesystem via pypdf (comptage de pages, présence dans le
    bon sous-répertoire : output/<trigger>/, output/no_code/, output/error/)

Ces tests nécessitent que le tester et pdf-dispatch partagent le même volume
/data (configuré par data_path dans config.yaml).

Classes :
  TestFiledropPageCounts   — vérification croisée API + filesystem
  TestConfigCorruption     — manipulation directe de .splitter_config.json
  TestDirectoryRobustness  — renommage / suppression de sous-répertoires
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

from file_dropper import FileDropper
from helpers import set_config, set_triggers
from pdf_generator import (
    fixture_one_trigger_after,
    fixture_one_trigger_before,
    fixture_two_triggers,
    make_pdf,
    make_single_page_with_code,
    make_truncated_pdf,
    make_unknown_trigger,
)

pytestmark = pytest.mark.filedrop

TRIGGER = "FK3"

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures de module
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def dropper(cfg, http, server, log):
    """FileDropper partagé pour tout le module."""
    data_path = cfg.get("data_path", "")
    if not data_path:
        pytest.skip("data_path non configuré — Phase 8 nécessite l'accès à /data")
    return FileDropper(Path(data_path), http, server, log)


@pytest.fixture(scope="module")
def data_path(cfg):
    """Path racine du répertoire /data."""
    p = cfg.get("data_path", "")
    if not p:
        pytest.skip("data_path non configuré")
    return Path(p)


@pytest.fixture(autouse=True)
def _reset(http, server):
    """Configuration de référence avant chaque test."""
    set_triggers(http, server, [
        {"value": TRIGGER, "page_handling": "keep", "case_sensitive": True}
    ])
    set_config(http, server,
               separator_placement="before",
               subdirs_by_trigger=True,
               delete_source=False)
    yield


@pytest.fixture(autouse=True)
def _cleanup_on_pass(dropper, request):
    """Supprimer les sorties uniquement si le test passe (garder pour inspection sinon)."""
    results: list = []
    yield results
    if hasattr(request.node, "rep_call") and request.node.rep_call.passed:
        for r in results:
            dropper.cleanup_output(r)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 8a — Vérification page-count (API + filesystem)
# ─────────────────────────────────────────────────────────────────────────────

class TestFiledropPageCounts:
    """
    Vérification croisée : chaque assertion repose sur DEUX sources indépendantes —
    les événements /api/state ET le comptage de pages pypdf sur les fichiers produits.

    Couvre des scénarios complémentaires à la Phase 1 (qui vérifie déjà status et
    docs_count) : page-count explicite pour les cas no_code, delete et multi-triggers.
    """

    def test_unknown_trigger_filesystem_and_page_count(
        self, dropper, _cleanup_on_pass, log
    ):
        """
        PDF avec code non dans la liste de triggers → no_code/ avec page-count correct.

        Structure du PDF : [page barcode "UNKNOWN_CODE_XYZ"] [contenu 2p]
        Attendu : 1 fichier dans no_code/, aucun dans les dossiers trigger,
                  page-count du fichier ≥ 1 (selon placement).
        """
        pdf = make_unknown_trigger("UNKNOWN_CODE_XYZ")
        r = dropper.drop(pdf, prefix="unk_trig")
        _cleanup_on_pass.append(r)

        assert r.status == "success"
        assert len(r.output_files) == 0, (
            f"Code inconnu ne doit pas atterrir dans un dossier trigger : {r.output_files}"
        )
        assert len(r.no_code_files) >= 1, "Code inconnu doit aller dans no_code/"

        for f in r.no_code_files:
            pages = dropper.page_count(f)
            assert pages >= 1, f"no_code/{f.name} : {pages} page(s), attendu ≥ 1"
            log.info(f"  no_code/{f.name}: {pages}p ✓")

    def test_no_code_pdf_filesystem_verification(
        self, dropper, _cleanup_on_pass, log
    ):
        """
        PDF sans aucun code-barres (3 pages de contenu pur) → no_code/ avec 3 pages.

        Vérification filesystem : le fichier produit doit avoir exactement 3 pages.
        """
        pdf = make_pdf([
            {"kind": "content", "text": "Document A — page 1"},
            {"kind": "content", "text": "Document A — page 2"},
            {"kind": "content", "text": "Document A — page 3"},
        ])
        r = dropper.drop(pdf, prefix="nocode3p")
        _cleanup_on_pass.append(r)

        assert r.status == "success"
        assert len(r.no_code_files) == 1
        assert len(r.output_files) == 0

        pages = dropper.page_count(r.no_code_files[0])
        assert pages == 3, f"PDF 3-pages sans code : attendu 3 pages, obtenu {pages}"
        log.info(f"  no_code/{r.no_code_files[0].name}: {pages}p ✓")

    def test_two_triggers_page_counts(
        self, dropper, http, server, _cleanup_on_pass, log
    ):
        """
        PDF [code FK3][contenu 2p][code FK3][contenu 1p] — placement=before, keep
        → 2 documents en sortie : doc1=3p (code+2), doc2=2p (code+1).

        Vérification double :
          • API : docs_count == 2
          • Filesystem : page-count exact de chaque document
        """
        set_triggers(http, server, [
            {"value": TRIGGER, "page_handling": "keep", "case_sensitive": True}
        ])
        set_config(http, server, separator_placement="before")

        r = dropper.drop(fixture_two_triggers(TRIGGER, TRIGGER), prefix="two_trig")
        _cleanup_on_pass.append(r)

        assert r.status == "success"
        # fixture_two_triggers : [content p1] [QR p2] [content p3-4] [QR p5] [content p6]
        # Page 1 (avant 1er trigger) va en no_code → 3 docs au total
        assert r.docs_count == 3, f"Attendu 3 docs (2 output + 1 no_code), obtenu {r.docs_count}"
        assert len(r.output_files) == 2
        assert len(r.no_code_files) == 1, "La page avant le 1er trigger doit aller en no_code"

        # Use output_files directly (sorted by name) to skip the no_code file
        # which carries the lowest counter and would be first in all_docs.
        pc0 = r.page_count(r.output_files[0])
        pc1 = r.page_count(r.output_files[1])
        assert pc0 == 3, f"Doc 1 (keep): attendu 3p, obtenu {pc0}"
        assert pc1 == 2, f"Doc 2 (keep): attendu 2p, obtenu {pc1}"
        log.info(f"  2 triggers keep : doc1={pc0}p, doc2={pc1}p ✓")

    def test_delete_mode_removes_separator_page(
        self, dropper, http, server, _cleanup_on_pass, log
    ):
        """
        PDF [code FK3][contenu 2p] — page_handling=delete
        → 1 document, la page séparateur est absente : page-count == 2.
        """
        set_triggers(http, server, [
            {"value": TRIGGER, "page_handling": "delete", "case_sensitive": True}
        ])

        r = dropper.drop(fixture_one_trigger_before(TRIGGER), prefix="del_sep")
        _cleanup_on_pass.append(r)

        assert r.status == "success"
        # fixture_one_trigger_before : [content p1] [QR p2] [content p3-4]
        # before+delete : p1→no_code, p2 (QR) supprimée, p3-4→output → 2 docs
        assert r.docs_count == 2, f"Attendu 2 docs (1 output + 1 no_code), obtenu {r.docs_count}"
        assert len(r.output_files) == 1
        assert len(r.no_code_files) == 1

        # output_files[0] is the trigger doc; no_code would be all_docs[0]
        pages = r.page_count(r.output_files[0])
        assert pages == 2, (
            f"Page séparateur doit être exclue en mode delete (attendu 2p, obtenu {pages})"
        )
        log.info(f"  delete mode : doc1={pages}p (page séparateur supprimée) ✓")

    def test_single_page_delete_produces_no_output(
        self, dropper, http, server, _cleanup_on_pass, log
    ):
        """
        PDF d'une seule page (= la page séparateur) avec page_handling=delete
        → aucun document produit : la seule page est supprimée.
        """
        set_triggers(http, server, [
            {"value": TRIGGER, "page_handling": "delete", "case_sensitive": True}
        ])

        r = dropper.drop(make_single_page_with_code(TRIGGER), prefix="one_del")
        _cleanup_on_pass.append(r)

        assert r.docs_count == 0, (
            f"PDF 1-page en mode delete : attendu 0 doc, obtenu {r.docs_count}"
        )
        assert len(r.output_files) == 0
        assert len(r.no_code_files) == 0
        log.info("  single-page delete : 0 doc produit ✓")

    def test_after_placement_page_counts(
        self, dropper, http, server, _cleanup_on_pass, log
    ):
        """
        PDF [contenu(p1)] [contenu(p2)] [code FK3(p3)] [contenu(p4)] — placement=after, keep
        → 2 documents : doc1=[p1,p2,p3]=3p (code gardé en fin), doc2=[p4]=1p.

        Vérification double :
          • API : docs_count == 2
          • Filesystem : page-count de chaque document
        """
        set_triggers(http, server, [
            {"value": TRIGGER, "page_handling": "keep", "case_sensitive": True}
        ])
        set_config(http, server, separator_placement="after")

        r = dropper.drop(fixture_one_trigger_after(TRIGGER), prefix="after_keep")
        _cleanup_on_pass.append(r)

        assert r.status == "success"
        assert r.docs_count == 2, (
            f"Placement after+keep : attendu 2 docs, obtenu {r.docs_count}"
        )

        pc0 = r.page_count_of(0)
        pc1 = r.page_count_of(1)
        assert pc0 == 3, f"Doc 1 (contenu+code) : attendu 3p, obtenu {pc0}"
        assert pc1 == 1, f"Doc 2 (contenu restant) : attendu 1p, obtenu {pc1}"
        log.info(f"  after/keep : doc1={pc0}p, doc2={pc1}p ✓")


# ─────────────────────────────────────────────────────────────────────────────
# Phase 8b — Corruption du fichier de configuration
# ─────────────────────────────────────────────────────────────────────────────

class TestConfigCorruption:
    """
    Manipulation directe de /data/.splitter_config.json pour simuler des
    interventions externes : suppression accidentelle, corruption JSON,
    champs manquants, valeurs hors limites.

    Chaque test vérifie la RÉSILIENCE du service : on n'exige pas le bon
    routage, mais que pdf-dispatch reste opérationnel et ne crashe pas.
    """

    @pytest.fixture(autouse=True)
    def _guard_config(self, data_path, log):
        """Sauvegarde et restaure .splitter_config.json autour de chaque test."""
        config_path = data_path / ".splitter_config.json"
        backup = config_path.read_bytes() if config_path.exists() else None
        if backup:
            log.info(f"Config sauvegardée ({len(backup)}B)")
        yield
        if backup is not None:
            config_path.write_bytes(backup)
            log.info("Config restaurée")
        elif config_path.exists():
            config_path.unlink()
        time.sleep(1.0)  # laisser pdf-dispatch recharger

    def _drop_and_assert_alive(self, dropper, _cleanup_on_pass, log, prefix):
        """Helper : déposer un PDF simple et vérifier que le service répond."""
        r = dropper.drop(fixture_one_trigger_before(TRIGGER), prefix=prefix)
        _cleanup_on_pass.append(r)
        assert r.status in ("success", "error", "unknown"), (
            f"Service doit rester opérationnel (got None/exception) — status={r.status!r}"
        )
        log.info(f"  Service opérationnel après corruption : status={r.status} ✓")
        return r

    def test_missing_config_service_survives(
        self, dropper, data_path, _cleanup_on_pass, log
    ):
        """Suppression de .splitter_config.json → service résilient."""
        config_path = data_path / ".splitter_config.json"
        if config_path.exists():
            config_path.unlink()
        log.info("Config supprimée")
        time.sleep(0.8)
        self._drop_and_assert_alive(dropper, _cleanup_on_pass, log, "no_cfg")

    def test_invalid_json_service_survives(
        self, dropper, data_path, _cleanup_on_pass, log
    ):
        """JSON invalide dans .splitter_config.json → service résilient."""
        (data_path / ".splitter_config.json").write_text(
            "{{{ CORRUPT JSON !!!}", encoding="utf-8"
        )
        log.info("Config corrompue (JSON invalide)")
        time.sleep(0.8)
        self._drop_and_assert_alive(dropper, _cleanup_on_pass, log, "bad_json")

    def test_empty_object_service_survives(
        self, dropper, data_path, _cleanup_on_pass, log
    ):
        """Objet JSON vide {} (champs manquants) → service résilient, défauts appliqués."""
        (data_path / ".splitter_config.json").write_text("{}", encoding="utf-8")
        log.info("Config remplacée par {} (tous champs manquants)")
        time.sleep(0.8)
        self._drop_and_assert_alive(dropper, _cleanup_on_pass, log, "empty_obj")

    def test_wrong_types_service_survives(
        self, dropper, data_path, _cleanup_on_pass, log
    ):
        """Valeurs de mauvais type dans .splitter_config.json → service résilient."""
        bad = {
            "split_values": "not_a_list",   # doit être une liste
            "barcode_dpi": "high",           # doit être un entier
            "separator_placement": 42,       # doit être "before" | "after"
            "counter": None,                 # doit être un entier
        }
        (data_path / ".splitter_config.json").write_text(
            json.dumps(bad), encoding="utf-8"
        )
        log.info(f"Config avec mauvais types injectée : {bad}")
        time.sleep(0.8)
        self._drop_and_assert_alive(dropper, _cleanup_on_pass, log, "bad_types")

    def test_config_restored_after_api_write(
        self, dropper, data_path, http, server, _cleanup_on_pass, log
    ):
        """
        Après corruption, une écriture via l'API (/api/config) doit
        rétablir un état cohérent, vérifiable par un dépôt de fichier.
        """
        # Corrompre
        (data_path / ".splitter_config.json").write_text(
            "null", encoding="utf-8"
        )
        time.sleep(0.5)

        # L'API doit ré-écrire une config valide
        set_config(http, server,
                   separator_placement="before",
                   subdirs_by_trigger=True)
        set_triggers(http, server, [
            {"value": TRIGGER, "page_handling": "keep", "case_sensitive": True}
        ])
        time.sleep(0.5)

        # Le traitement doit maintenant fonctionner normalement
        r = dropper.drop(fixture_one_trigger_before(TRIGGER), prefix="after_restore")
        _cleanup_on_pass.append(r)
        assert r.status == "success", (
            f"Après restauration API, le traitement doit réussir (got {r.status!r})"
        )
        log.info(f"  Restauration via API : status={r.status} ✓")


# ─────────────────────────────────────────────────────────────────────────────
# Phase 8c — Robustesse des répertoires
# ─────────────────────────────────────────────────────────────────────────────

class TestDirectoryRobustness:
    """
    Tests de résilience du watchdog face à des modifications du filesystem :
    renommage d'un sous-dossier de sortie, suppression de répertoires
    standard (no_code/, error/).
    """

    def test_trigger_subdir_rename_does_not_break_watchdog(
        self, dropper, data_path, http, server, _cleanup_on_pass, log
    ):
        """
        Renommer output/FK3/ → output/FK3_old/ ne doit pas casser le traitement :
        pdf-dispatch doit recréer output/FK3/ sur le dépôt suivant.
        """
        # Créer output/FK3/ via un premier dépôt
        r1 = dropper.drop(fixture_one_trigger_before(TRIGGER), prefix="pre_ren")
        _cleanup_on_pass.append(r1)
        assert r1.status == "success"

        fk3_dir = data_path / "output" / TRIGGER
        fk3_old = data_path / "output" / f"{TRIGGER}_renamed"
        try:
            if fk3_dir.exists():
                fk3_dir.rename(fk3_old)
                log.info(f"Renommé output/{TRIGGER}/ → output/{TRIGGER}_renamed/")

            # Le watchdog doit recréer le dossier et traiter normalement
            r2 = dropper.drop(fixture_one_trigger_before(TRIGGER), prefix="post_ren")
            _cleanup_on_pass.append(r2)
            assert r2.status in ("success", "unknown"), (
                f"Traitement doit continuer après renommage du dossier trigger : {r2.status!r}"
            )
            if r2.status == "success":
                assert fk3_dir.exists(), "output/FK3/ doit être recréé"
            log.info(f"  Après renommage : status={r2.status} ✓")
        finally:
            if fk3_old.exists():
                shutil.rmtree(fk3_old, ignore_errors=True)

    def test_no_code_dir_deleted_and_recreated(
        self, dropper, data_path, _cleanup_on_pass, log
    ):
        """
        Suppression de output/no_code/ → pdf-dispatch doit le recréer
        et continuer à y router les PDFs sans code.
        """
        no_code = data_path / "output" / "no_code"
        if no_code.exists():
            shutil.rmtree(no_code)
            log.info("output/no_code/ supprimé")
        time.sleep(0.3)

        pdf = make_pdf([{"kind": "content", "text": "Sans code, no_code/ absent"}])
        r = dropper.drop(pdf, prefix="nc_after_del")
        _cleanup_on_pass.append(r)

        assert no_code.exists(), "output/no_code/ doit être recréé par pdf-dispatch"
        assert r.status in ("success", "unknown"), (
            f"Traitement doit continuer : {r.status!r}"
        )
        log.info(f"  Après suppression no_code/ : status={r.status}, "
                 f"recréé={no_code.exists()} ✓")

    def test_error_dir_deleted_and_recreated(
        self, dropper, data_path, _cleanup_on_pass, log
    ):
        """
        Suppression de output/error/ → pdf-dispatch doit le recréer
        quand un PDF invalide est déposé.
        """
        error_dir = data_path / "output" / "error"
        if error_dir.exists():
            shutil.rmtree(error_dir)
            log.info("output/error/ supprimé")
        time.sleep(0.3)

        r = dropper.drop(make_truncated_pdf(), prefix="err_after_del")
        _cleanup_on_pass.append(r)

        assert error_dir.exists(), "output/error/ doit être recréé par pdf-dispatch"
        assert r.status in ("error", "unknown"), (
            f"PDF corrompu doit produire une erreur : {r.status!r}"
        )
        log.info(f"  Après suppression error/ : status={r.status}, "
                 f"recréé={error_dir.exists()}, "
                 f"error_files={len(r.error_files)} ✓")

    def test_multiple_restores_sequential(
        self, dropper, data_path, _cleanup_on_pass, log
    ):
        """
        Suppression successive de no_code/ et error/ → le watchdog
        recrée les deux répertoires de manière indépendante.
        """
        no_code  = data_path / "output" / "no_code"
        error_d  = data_path / "output" / "error"

        for d in (no_code, error_d):
            if d.exists():
                shutil.rmtree(d)
        time.sleep(0.3)

        # Déclencher la recréation de no_code/
        pdf_nc = make_pdf([{"kind": "content", "text": "Recréation no_code/"}])
        r_nc = dropper.drop(pdf_nc, prefix="seq_nc")
        _cleanup_on_pass.append(r_nc)

        # Déclencher la recréation de error/
        r_err = dropper.drop(make_truncated_pdf(), prefix="seq_err")
        _cleanup_on_pass.append(r_err)

        assert no_code.exists(), "no_code/ doit être recréé"
        assert error_d.exists(), "error/ doit être recréé"
        log.info(f"  Suppressions séquentielles : "
                 f"no_code recréé={no_code.exists()}, "
                 f"error recréé={error_d.exists()} ✓")


# ─────────────────────────────────────────────────────────────────────────────
# Phase 8d — Persistance de la configuration sur disque
# ─────────────────────────────────────────────────────────────────────────────

class TestConfigPersistence:
    """Vérifie que les changements de configuration via l'API sont bien
    écrits dans /data/.splitter_config.json.

    Ces tests simulent le cas où le container redémarre : si la config
    n'est pas persistée sur disque, elle serait perdue au redémarrage.
    On ne redémarre pas réellement le container — on lit directement le
    fichier JSON pour s'assurer que chaque valeur modifiée est sauvegardée.
    """

    @pytest.fixture(autouse=True)
    def _restore(self, http, server, data_path, log):
        """Restaure la config à l'état de départ après chaque test."""
        # Capture l'état initial
        orig = json.loads((data_path / ".splitter_config.json").read_text())             if (data_path / ".splitter_config.json").exists() else {}
        yield
        # Restaurer via API (plus sûr qu'une écriture directe)
        set_config(http, server,
                   separator_placement=orig.get("separator_placement", "before"),
                   subdirs_by_trigger=orig.get("subdirs_by_trigger", True),
                   delete_source=orig.get("delete_source", False))
        set_triggers(http, server, orig.get("split_values", []))
        time.sleep(0.3)

    def _read_cfg(self, data_path) -> dict:
        """Lit et parse .splitter_config.json."""
        cfg_path = data_path / ".splitter_config.json"
        assert cfg_path.exists(), ".splitter_config.json must exist"
        return json.loads(cfg_path.read_text(encoding="utf-8"))

    def test_trigger_persisted_to_disk(self, http, server, data_path, log):
        """Ajouter un trigger via API → apparaît dans .splitter_config.json."""
        unique_val = f"PERSIST_{int(time.time())}"
        set_triggers(http, server, [
            {"value": unique_val, "page_handling": "keep", "case_sensitive": True}
        ])
        time.sleep(0.5)
        saved = self._read_cfg(data_path)
        values = [
            (sv["value"] if isinstance(sv, dict) else sv)
            for sv in saved.get("split_values", [])
        ]
        assert unique_val in values, (
            f"{unique_val!r} not found in persisted split_values: {values}"
        )
        log.info(f"Trigger {unique_val!r} trouvé dans la config sur disque ✓")

    def test_separator_placement_persisted(self, http, server, data_path, log):
        """Changer separator_placement via API → persisté sur disque."""
        set_config(http, server, separator_placement="after")
        time.sleep(0.5)
        saved = self._read_cfg(data_path)
        assert saved.get("separator_placement") == "after", (
            f"separator_placement not persisted: {saved.get('separator_placement')!r}"
        )
        log.info("separator_placement='after' persisté ✓")

    def test_subdirs_flag_persisted(self, http, server, data_path, log):
        """subdirs_by_trigger=False → persisté sur disque."""
        set_config(http, server, subdirs_by_trigger=False)
        time.sleep(0.5)
        saved = self._read_cfg(data_path)
        assert saved.get("subdirs_by_trigger") == False, (
            f"subdirs_by_trigger not persisted: {saved.get('subdirs_by_trigger')!r}"
        )
        log.info("subdirs_by_trigger=False persisté ✓")

    def test_multiple_config_fields_persisted_atomically(
        self, http, server, data_path, log
    ):
        """Plusieurs champs modifiés ensemble → tous persistés."""
        unique_val = f"ATOM_{int(time.time())}"
        set_triggers(http, server, [
            {"value": unique_val, "page_handling": "delete", "case_sensitive": False}
        ])
        set_config(http, server, separator_placement="after",
                   subdirs_by_trigger=False, delete_source=True)
        time.sleep(0.5)
        saved = self._read_cfg(data_path)

        values = [
            (sv["value"] if isinstance(sv, dict) else sv)
            for sv in saved.get("split_values", [])
        ]
        assert unique_val in values, f"Trigger not persisted: {values}"
        assert saved.get("separator_placement") == "after"
        assert saved.get("subdirs_by_trigger") == False
        assert saved.get("delete_source") == True
        log.info("Tous les champs persistés atomiquement ✓")

    def test_clear_triggers_persisted(self, http, server, data_path, log):
        """Vider la liste de triggers → split_values=[] sur disque."""
        # Ajouter d'abord un trigger
        set_triggers(http, server, [{"value": "TODEL", "page_handling": "keep"}])
        time.sleep(0.2)
        # Le supprimer
        set_triggers(http, server, [])
        time.sleep(0.5)
        saved = self._read_cfg(data_path)
        values = saved.get("split_values", [])
        assert values == [], (
            f"Empty trigger list must be persisted as []: got {values!r}"
        )
        log.info("Liste vide persistée ✓")
