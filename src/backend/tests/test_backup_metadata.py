"""Tests für utils/backup_metadata.py.

Das Modul greift bewusst weder auf die Datenbank noch auf die API zu. Diese Datei
darf deshalb nichts importieren, was database.connection lädt.
"""
import hashlib
import io
import json
import zipfile

import pytest

from utils import backup_metadata as bm


def _manifest(**aenderungen) -> dict:
    daten = bm.erstelle_manifest(
        backup_type="manual",
        app_version="0.7.8",
        schema_version=181,
        profile_id="profil-a",
        profile_name="Standard",
        created_at="2026-11-07T19:45:00Z",
        completed_at="2026-11-07T19:45:06Z",
        contents={"format": "sqlite", "database": True, "uploads": True, "file_count": 2},
        integrity_files={"rechnungsfee.db": "a" * 64},
    )
    daten.update(aenderungen)
    return daten


# ── Atomares Schreiben ───────────────────────────────────────────────────────

def test_schreibe_atomar_legt_datei_mit_inhalt_an(tmp_path):
    ziel = tmp_path / "backup.zip"

    bm.schreibe_atomar(ziel, b"inhalt")

    assert ziel.read_bytes() == b"inhalt"


def test_schreibe_atomar_laesst_bei_fehler_ziel_und_keine_reste_zurueck(tmp_path, monkeypatch):
    ziel = tmp_path / "backup.zip"
    ziel.write_bytes(b"alt")

    def scheitert(*_args):
        raise OSError("Datenträger voll")

    monkeypatch.setattr(bm.os, "replace", scheitert)

    with pytest.raises(OSError):
        bm.schreibe_atomar(ziel, b"neu")

    assert ziel.read_bytes() == b"alt"
    assert [p.name for p in tmp_path.iterdir()] == ["backup.zip"]


# ── Profilkennung ────────────────────────────────────────────────────────────

def test_profile_id_bleibt_ueber_mehrere_aufrufe_stabil(tmp_path):
    erste = bm.lade_oder_erzeuge_profile_id(tmp_path)

    assert erste
    assert bm.lade_oder_erzeuge_profile_id(tmp_path) == erste


def test_uebernommene_profile_id_ersetzt_die_bisherige(tmp_path):
    bm.lade_oder_erzeuge_profile_id(tmp_path)

    bm.setze_profile_id(tmp_path, "aus-dem-backup")

    assert bm.lade_oder_erzeuge_profile_id(tmp_path) == "aus-dem-backup"


def test_leere_profile_id_datei_erzeugt_eine_neue_kennung(tmp_path):
    (tmp_path / bm.PROFILE_ID_DATEI).write_text("  \n", encoding="utf-8")

    assert bm.lade_oder_erzeuge_profile_id(tmp_path).strip()


# ── Manifest ─────────────────────────────────────────────────────────────────

def test_erstelltes_manifest_ist_gueltig():
    assert bm.manifest_fehler(_manifest()) == []


def test_jedes_manifest_bekommt_eine_eigene_backup_id():
    assert _manifest()["backup_id"] != _manifest()["backup_id"]


@pytest.mark.parametrize("feld", [
    "backup_id", "app_version", "schema_version", "backup_type",
    "created_at", "completed_at", "contents", "integrity",
])
def test_fehlendes_pflichtfeld_macht_manifest_ungueltig(feld):
    daten = _manifest()
    del daten[feld]

    assert bm.manifest_fehler(daten)


@pytest.mark.parametrize("aenderung", [
    {"backup_type": "unbekannt"},
    {"schema_version": "181"},
    {"schema_version": True},
    {"completed_at": "kein-datum"},
    {"completed_at": "2026-11-07T19:45:06"},
    {"integrity": {"algorithm": "md5", "files": {}}},
    {"integrity": {"algorithm": "sha256", "files": ["rechnungsfee.db"]}},
])
def test_falscher_feldwert_macht_manifest_ungueltig(aenderung):
    assert bm.manifest_fehler(_manifest(**aenderung))


def test_manifest_ohne_objekt_ist_ungueltig():
    assert bm.manifest_fehler(["kein", "objekt"])


# ── Verlauf ──────────────────────────────────────────────────────────────────

def test_verlauf_gibt_eintraege_in_schreibreihenfolge_zurueck(tmp_path):
    pfad = tmp_path / bm.VERLAUF_DATEI

    bm.verlauf_anhaengen(pfad, {"backup_id": "a"})
    bm.verlauf_anhaengen(pfad, {"backup_id": "b"})

    assert [e["backup_id"] for e in bm.lies_verlauf(pfad)] == ["a", "b"]


def test_fehlender_verlauf_ist_leer(tmp_path):
    assert bm.lies_verlauf(tmp_path / bm.VERLAUF_DATEI) == []


def test_kaputte_verlaufszeilen_werden_uebersprungen(tmp_path):
    pfad = tmp_path / bm.VERLAUF_DATEI
    pfad.write_text('{"backup_id": "a"}\nkein json\n[1, 2]\n\n{"backup_id": "b"}\n', encoding="utf-8")

    assert [e["backup_id"] for e in bm.lies_verlauf(pfad)] == ["a", "b"]


def test_verlauf_behaelt_nur_die_neuesten_eintraege(tmp_path):
    pfad = tmp_path / bm.VERLAUF_DATEI

    for nummer in range(5):
        bm.verlauf_anhaengen(pfad, {"backup_id": str(nummer)}, max_eintraege=3)

    assert [e["backup_id"] for e in bm.lies_verlauf(pfad)] == ["2", "3", "4"]


# ── Sortierung ───────────────────────────────────────────────────────────────

def _eintrag(backup_id, completed_at, backup_type="manual", umfang="voll"):
    return {"backup_id": backup_id, "completed_at": completed_at,
            "backup_type": backup_type, "umfang": umfang}


def test_sortierung_nutzt_den_zeitpunkt_nicht_den_text():
    frueher = _eintrag("a", "2026-11-07T21:00:00+02:00")  # 19:00 UTC
    spaeter = _eintrag("b", "2026-11-07T20:00:00Z")

    assert bm.sortiere_neueste_zuerst([frueher, spaeter]) == [spaeter, frueher]


def test_gleicher_zeitpunkt_wird_ueber_die_backup_id_entschieden():
    a = _eintrag("a", "2026-11-07T20:00:00Z")
    b = _eintrag("b", "2026-11-07T20:00:00Z")

    assert bm.sortiere_neueste_zuerst([a, b]) == [b, a]
    assert bm.sortiere_neueste_zuerst([b, a]) == [b, a]


def test_eintraege_ohne_gueltigen_zeitpunkt_stehen_am_ende():
    ohne = _eintrag("z", None)
    kaputt = _eintrag("y", "kein-datum")
    mit = _eintrag("a", "2026-11-07T20:00:00Z")

    assert bm.sortiere_neueste_zuerst([ohne, kaputt, mit])[0] == mit


def test_neueste_vollsicherung_ignoriert_reine_datenbank_snapshots():
    voll = _eintrag("a", "2026-11-07T19:00:00Z")
    nur_db = _eintrag("b", "2026-11-07T20:00:00Z", umfang="nur_db")

    assert bm.neueste_vollsicherung([nur_db, voll]) == voll


@pytest.mark.parametrize("backup_type", ["pre_restore", "pre_migration"])
def test_sicherheitsbackup_gilt_nie_als_neueste_vollsicherung(backup_type):
    voll = _eintrag("a", "2026-11-07T19:00:00Z")
    sicherheit = _eintrag("b", "2026-11-07T20:00:00Z", backup_type=backup_type)

    assert bm.neueste_vollsicherung([sicherheit, voll]) == voll


def test_ohne_vollsicherung_gibt_es_keine_neueste():
    assert bm.neueste_vollsicherung([_eintrag("a", "2026-11-07T19:00:00Z", umfang="nur_db")]) is None


# ── Archivprüfung ────────────────────────────────────────────────────────────

DB_INHALT = b"datenbank"
DB_HASH = hashlib.sha256(DB_INHALT).hexdigest()


def _archiv(eintraege: dict) -> zipfile.ZipFile:
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w") as zf:
        for name, inhalt in eintraege.items():
            zf.writestr(name, inhalt)
    puffer.seek(0)
    return zipfile.ZipFile(puffer)


def _archiv_mit_manifest(db=DB_INHALT, uploads=True, **aenderungen) -> zipfile.ZipFile:
    daten = _manifest(**aenderungen)
    daten.setdefault("integrity", {})
    if "integrity" not in aenderungen:
        daten["integrity"]["files"] = {"rechnungsfee.db": DB_HASH}
    eintraege = {"rechnungsfee.db": db, bm.MANIFEST_NAME: json.dumps(daten)}
    if uploads:
        eintraege["uploads/beleg.pdf"] = b"x"
    return _archiv(eintraege)


def _pruefe(zf, **optionen):
    optionen.setdefault("schema_version", 181)
    return bm.pruefe_archiv(zf, **optionen)


def _codes(eintraege) -> set:
    return {e["code"] for e in eintraege}


def test_einwandfreies_backup_hat_keine_hinweise():
    ergebnis = _pruefe(_archiv_mit_manifest(), profile_id="profil-a")

    assert ergebnis.blockaden == []
    assert ergebnis.warnungen == []
    assert ergebnis.infos == []
    assert ergebnis.manifest["backup_type"] == "manual"


def test_backup_ohne_datenbank_wird_blockiert():
    ergebnis = _pruefe(_archiv({"uploads/beleg.pdf": b"x"}))

    assert "keine_datenbank" in _codes(ergebnis.blockaden)


def test_veraenderte_datenbank_wird_blockiert():
    ergebnis = _pruefe(_archiv_mit_manifest(db=b"manipuliert"))

    assert "hash_abweichung" in _codes(ergebnis.blockaden)


def test_im_manifest_genannte_aber_fehlende_datei_wird_blockiert():
    files = {"rechnungsfee.db": DB_HASH, "database/fehlt.jsonl": "b" * 64}
    ergebnis = _pruefe(_archiv_mit_manifest(integrity={"algorithm": "sha256", "files": files}))

    assert "hash_abweichung" in _codes(ergebnis.blockaden)


def test_neueres_schema_wird_blockiert():
    ergebnis = _pruefe(_archiv_mit_manifest(schema_version=182))

    assert "schema_zu_neu" in _codes(ergebnis.blockaden)


def test_aelteres_schema_ist_nur_ein_hinweis():
    ergebnis = _pruefe(_archiv_mit_manifest(schema_version=180), profile_id="profil-a")

    assert ergebnis.blockaden == []
    assert ergebnis.warnungen == []
    assert _codes(ergebnis.infos) == {"schema_aelter"}


def test_neuere_manifest_version_wird_blockiert():
    ergebnis = _pruefe(_archiv_mit_manifest(manifest_version=bm.MANIFEST_VERSION + 1))

    assert "manifest_zu_neu" in _codes(ergebnis.blockaden)


@pytest.mark.parametrize("name", [
    "uploads/../../boese.txt",
    "uploads/unter/../../../boese.txt",
    "uploads/..\\..\\boese.txt",
    "uploads//etc/boese.txt",
    "uploads/C:/boese.txt",
])
def test_upload_pfad_ausserhalb_von_uploads_wird_blockiert(name):
    ergebnis = _pruefe(_archiv({"rechnungsfee.db": DB_INHALT, name: b"x"}))

    assert "unsicherer_pfad" in _codes(ergebnis.blockaden)


def test_windows_trenner_im_upload_pfad_ist_erlaubt():
    ergebnis = _pruefe(_archiv({"rechnungsfee.db": DB_INHALT, "uploads/rechnungen\\a.pdf": b"x"}))

    assert "unsicherer_pfad" not in _codes(ergebnis.blockaden)


def test_altes_backup_ohne_manifest_bleibt_einspielbar():
    ergebnis = _pruefe(_archiv({"rechnungsfee.db": b"fake-sqlite-bytes", "uploads/a.txt": b"x"}))

    assert ergebnis.blockaden == []
    assert ergebnis.manifest is None
    assert _codes(ergebnis.warnungen) == {"manifest_fehlt", "schema_unbekannt"}


@pytest.mark.parametrize("inhalt", [
    "{kaputt",
    '{"manifest_version": 1}',
    '["kein", "objekt"]',
    b"\xff\xfe",
])
def test_defektes_manifest_warnt_nur(inhalt):
    zf = _archiv({"rechnungsfee.db": DB_INHALT, bm.MANIFEST_NAME: inhalt, "uploads/a.txt": b"x"})

    ergebnis = _pruefe(zf)

    assert ergebnis.blockaden == []
    assert ergebnis.manifest is None
    assert "manifest_defekt" in _codes(ergebnis.warnungen)


def test_uebergrosses_manifest_gilt_als_defekt():
    zf = _archiv({"rechnungsfee.db": DB_INHALT, bm.MANIFEST_NAME: " " * (bm.MAX_MANIFEST_BYTES + 1)})

    assert "manifest_defekt" in _codes(_pruefe(zf).warnungen)


def test_schema_wird_ohne_manifest_aus_dem_sqlite_header_gelesen(tmp_path):
    import sqlite3
    pfad = tmp_path / "echt.db"
    verbindung = sqlite3.connect(pfad)
    verbindung.execute("PRAGMA user_version = 182")
    verbindung.execute("CREATE TABLE t (id INTEGER)")
    verbindung.commit()
    verbindung.close()

    ergebnis = _pruefe(_archiv({"rechnungsfee.db": pfad.read_bytes()}))

    assert "schema_zu_neu" in _codes(ergebnis.blockaden)


def test_schema_wird_ohne_manifest_aus_dem_portablen_manifest_gelesen():
    zf = _archiv({"database/manifest.json": json.dumps({"schema_version": 180})})

    ergebnis = _pruefe(zf)

    assert ergebnis.blockaden == []
    assert "schema_aelter" in _codes(ergebnis.infos)


def test_anderes_profil_warnt():
    ergebnis = _pruefe(_archiv_mit_manifest(), profile_id="profil-b")

    assert "anderes_profil" in _codes(ergebnis.warnungen)


def test_ohne_eigenes_profil_gibt_es_keine_profilwarnung():
    assert "anderes_profil" not in _codes(_pruefe(_archiv_mit_manifest()).warnungen)


def test_bekanntes_neueres_backup_warnt():
    ergebnis = _pruefe(_archiv_mit_manifest(), neuester_zeitpunkt="2026-11-08T08:00:00Z")

    assert "neueres_backup_bekannt" in _codes(ergebnis.warnungen)


def test_das_neueste_bekannte_backup_selbst_warnt_nicht():
    ergebnis = _pruefe(_archiv_mit_manifest(), neuester_zeitpunkt="2026-11-07T19:45:06Z")

    assert "neueres_backup_bekannt" not in _codes(ergebnis.warnungen)


def test_backup_ohne_uploads_warnt():
    ergebnis = _pruefe(_archiv_mit_manifest(uploads=False))

    assert "nur_datenbank" in _codes(ergebnis.warnungen)
