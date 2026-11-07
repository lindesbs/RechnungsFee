"""
Anbindung der Backup-Metadaten an Erstellung, Liste und Wiederherstellung
(docs/superpowers/specs/2026-11-07-backup-metadaten-design.md).
"""
import asyncio
import hashlib
import io
import json
import sqlite3
import zipfile

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import main
from api import backup as backup_api
from database.connection import Base
from database.models import Unternehmen
from utils import backup_metadata as bm
from utils import backup_zip


def _lege_db_an(pfad, user_version=main.SCHEMA_VERSION):
    verbindung = sqlite3.connect(pfad)
    verbindung.execute("PRAGMA journal_mode=WAL")  # wie die echte Datenbank
    verbindung.execute("CREATE TABLE IF NOT EXISTS probe (wert TEXT)")
    verbindung.execute(f"PRAGMA user_version = {user_version}")
    verbindung.commit()
    verbindung.close()


def _db_bytes(tmp_path, user_version=main.SCHEMA_VERSION):
    pfad = tmp_path / f"quelle_{user_version}.db"
    _lege_db_an(pfad, user_version)
    return pfad.read_bytes()


def _upload(name, inhalt):
    return UploadFile(filename=name, file=io.BytesIO(inhalt))


def _verlauf(profil_dir):
    return bm.lies_verlauf(profil_dir / "backups" / bm.VERLAUF_DATEI)


def _zip_mit(eintraege):
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w") as zf:
        for name, inhalt in eintraege.items():
            zf.writestr(name, inhalt)
    return puffer.getvalue()


@pytest.fixture
def profil(tmp_path, monkeypatch):
    """Profilordner mit echter SQLite-Datei, in main und api/backup eingehängt."""
    db_path = tmp_path / "rechnungsfee.db"
    _lege_db_an(db_path)
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(main, "APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "RESTORE_MARKER_ZIP", tmp_path / "restore_pending.zip")
    monkeypatch.setattr(main, "RESTORE_MARKER_DB", tmp_path / "restore_pending.db")
    monkeypatch.setattr(backup_api, "DB_PATH", db_path)
    monkeypatch.setattr(backup_api, "APP_DATA_DIR", tmp_path)
    return tmp_path


# ── ZIP-Erstellung ────────────────────────────────────────────────────────────

def test_zip_enthaelt_gueltiges_manifest_als_letzten_eintrag(profil):
    (profil / "uploads" / "belege").mkdir(parents=True)
    (profil / "uploads" / "belege" / "a.pdf").write_bytes(b"pdf")

    inhalt, manifest = backup_zip.erstelle_backup_zip(profil / "rechnungsfee.db", profil, "manual")

    with zipfile.ZipFile(io.BytesIO(inhalt)) as zf:
        assert zf.namelist()[-1] == bm.MANIFEST_NAME
        assert json.loads(zf.read(bm.MANIFEST_NAME)) == manifest
        assert "uploads/belege/a.pdf" in zf.namelist()
        erwartet = hashlib.sha256(zf.read("rechnungsfee.db")).hexdigest()
    assert bm.manifest_fehler(manifest) == []
    assert manifest["integrity"]["files"] == {"rechnungsfee.db": erwartet}
    assert manifest["schema_version"] == main.SCHEMA_VERSION
    assert manifest["contents"] == {"format": "sqlite", "database": True, "uploads": True, "file_count": 2}
    assert manifest["profile_name"] == profil.name
    assert manifest["profile_id"] == (profil / bm.PROFILE_ID_DATEI).read_text()


def test_neues_zip_besteht_die_archivpruefung(profil):
    inhalt, _ = backup_zip.erstelle_backup_zip(profil / "rechnungsfee.db", profil, "manual")

    with zipfile.ZipFile(io.BytesIO(inhalt)) as zf:
        ergebnis = bm.pruefe_archiv(zf, schema_version=main.SCHEMA_VERSION)

    assert ergebnis.blockaden == []
    assert ergebnis.manifest is not None


def test_app_version_ist_ohne_build_datei_dev():
    assert backup_zip.app_version() == "dev"


# ── Verlauf ───────────────────────────────────────────────────────────────────

def test_lokaler_snapshot_steht_im_verlauf(profil):
    main._backup_datenbank("pre_migration")

    (eintrag,) = _verlauf(profil)
    datei = next((profil / "backups").glob("rechnungsfee_*.db"))
    assert eintrag["dateiname"] == datei.name
    assert eintrag["ort"] == "lokal"
    assert eintrag["umfang"] == "nur_db"
    assert eintrag["backup_type"] == "pre_migration"
    assert eintrag["schema_version"] == main.SCHEMA_VERSION
    assert eintrag["groesse"] == datei.stat().st_size
    assert eintrag["integrity"]["files"][datei.name] == hashlib.sha256(datei.read_bytes()).hexdigest()


def test_defekter_verlauf_verhindert_kein_backup(profil, monkeypatch):
    def scheitert(*_args, **_kwargs):
        raise OSError("Datenträger voll")
    monkeypatch.setattr(bm, "verlauf_anhaengen", scheitert)

    main._backup_datenbank("exit")

    assert list((profil / "backups").glob("rechnungsfee_*.db"))


def test_download_liefert_manifest_und_steht_im_verlauf(profil):
    antwort = backup_api.backup_download()

    async def lies():
        return b"".join([teil async for teil in antwort.body_iterator])

    with zipfile.ZipFile(io.BytesIO(asyncio.run(lies()))) as zf:
        manifest = json.loads(zf.read(bm.MANIFEST_NAME))
    (eintrag,) = _verlauf(profil)
    assert manifest["backup_type"] == "manual"
    assert eintrag["ort"] == "download"
    assert eintrag["umfang"] == "voll"
    assert eintrag["backup_id"] == manifest["backup_id"]


def test_extern_nur_erfolgreiche_ziele_im_verlauf(tmp_path, monkeypatch):
    db_path = tmp_path / "rechnungsfee.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(main, "APP_DATA_DIR", tmp_path)
    gut = tmp_path / "gut"
    kaputt = tmp_path / "datei_statt_ordner"
    kaputt.write_text("x")
    session = sessionmaker(bind=engine)()
    session.add(Unternehmen(
        firmenname="Test GmbH", strasse="Teststr.", hausnummer="1", plz="12345", ort="Testort",
        backup_extern_pfad_1=str(gut), backup_extern_pfad_2=str(kaputt),
        backup_extern_passwort="test-passwort-123",
        backup_extern_pfad_1_lokal_ok=True, backup_extern_pfad_2_lokal_ok=True,
    ))
    session.commit()
    session.close()

    ergebnis = main.backup_erstellen()

    assert ergebnis["fehler"]
    eintraege = _verlauf(tmp_path)
    extern = [e for e in eintraege if e["ort"] == "extern"]
    assert [e["ziel"] for e in extern] == [str(gut)]
    assert extern[0]["backup_type"] == "exit"
    assert extern[0]["umfang"] == "voll"
    assert extern[0]["dateiname"].endswith(".zip.enc")
    assert [e["backup_type"] for e in eintraege if e["ort"] == "lokal"] == ["exit"]


# ── Liste ─────────────────────────────────────────────────────────────────────

def _eintrag(**werte):
    return {
        "backup_id": "id", "dateiname": "x.zip.enc", "ort": "extern", "ziel": "/mnt/nas",
        "backup_type": "exit", "umfang": "voll", "created_at": "2026-11-01T10:00:00Z",
        "completed_at": "2026-11-01T10:00:05Z", "app_version": "0.7.8", "schema_version": 181,
        "profile_id": "p", "groesse": 10, "integrity": {"algorithm": "sha256", "files": {}},
        **werte,
    }


def _schreibe_verlauf(profil, *eintraege):
    for eintrag in eintraege:
        bm.verlauf_anhaengen(profil / "backups" / bm.VERLAUF_DATEI, eintrag)


def test_liste_zeigt_alte_snapshots_ohne_metadaten_weiter_an(profil):
    (profil / "backups").mkdir()
    (profil / "backups" / "rechnungsfee_20260613_143022.db").write_bytes(b"x")

    (eintrag,) = backup_api.backup_liste()

    assert eintrag["dateiname"] == "rechnungsfee_20260613_143022.db"
    assert eintrag["timestamp"] == "2026-06-13T14:30:22"
    assert eintrag["hat_metadaten"] is False
    assert eintrag["wiederherstellbar"] is True
    assert eintrag["ort"] == "lokal"
    assert eintrag["ist_neueste_vollsicherung"] is False


def test_liste_verbindet_snapshot_mit_verlauf(profil):
    (profil / "backups").mkdir()
    (profil / "backups" / "rechnungsfee_20261101_100000.db").write_bytes(b"x")
    _schreibe_verlauf(profil, _eintrag(
        dateiname="rechnungsfee_20261101_100000.db", ort="lokal", ziel=None, umfang="nur_db"))

    (eintrag,) = backup_api.backup_liste()

    assert eintrag["hat_metadaten"] is True
    assert eintrag["backup_type"] == "exit"
    assert eintrag["umfang"] == "nur_db"
    assert eintrag["app_version"] == "0.7.8"
    assert eintrag["schema_version"] == 181


def test_liste_zeigt_externe_eintraege_nur_zur_anzeige(profil):
    _schreibe_verlauf(profil, _eintrag())

    (eintrag,) = backup_api.backup_liste()

    assert eintrag["ort"] == "extern"
    assert eintrag["wiederherstellbar"] is False
    assert eintrag["timestamp"] == "2026-11-01T10:00:05Z"


def test_liste_laesst_rotierte_lokale_snapshots_weg(profil):
    _schreibe_verlauf(profil, _eintrag(dateiname="rechnungsfee_alt.db", ort="lokal", umfang="nur_db"))

    assert backup_api.backup_liste() == []


def test_liste_markiert_neueste_vollsicherung_und_nie_sicherheitsbackups(profil):
    _schreibe_verlauf(
        profil,
        _eintrag(backup_id="alt", dateiname="alt.zip.enc", completed_at="2026-11-01T10:00:00Z"),
        _eintrag(backup_id="neu", dateiname="neu.zip.enc", completed_at="2026-11-02T10:00:00Z"),
        _eintrag(backup_id="sicher", dateiname="sicher.zip.enc", backup_type="pre_restore",
                 completed_at="2026-11-03T10:00:00Z"),
    )

    liste = backup_api.backup_liste()

    assert [e["dateiname"] for e in liste] == ["sicher.zip.enc", "neu.zip.enc", "alt.zip.enc"]
    assert [e["ist_neueste_vollsicherung"] for e in liste] == [False, True, False]


# ── Prüfung vor dem Einspielen ────────────────────────────────────────────────

def test_inspect_liefert_metadaten_und_schreibt_nichts(profil):
    inhalt, manifest = backup_zip.erstelle_backup_zip(profil / "rechnungsfee.db", profil, "manual")
    (profil / bm.PROFILE_ID_DATEI).unlink()

    ergebnis = asyncio.run(main.backup_inspect(datei=_upload("backup.zip", inhalt), passwort=""))

    assert ergebnis["metadaten"]["backup_id"] == manifest["backup_id"]
    assert "integrity" not in ergebnis["metadaten"]
    assert ergebnis["blockaden"] == []
    assert ergebnis["verschluesselt"] is False
    assert not (profil / bm.PROFILE_ID_DATEI).exists()
    assert not main.RESTORE_MARKER_ZIP.exists()


def test_inspect_prueft_verschluesselte_backups_nach_passworteingabe(profil):
    inhalt, manifest = backup_zip.erstelle_backup_zip(profil / "rechnungsfee.db", profil, "manual")
    verschluesselt = main._encrypt_bytes(inhalt, "geheim-123")

    ergebnis = asyncio.run(main.backup_inspect(datei=_upload("b.zip.enc", verschluesselt), passwort="geheim-123"))

    assert ergebnis["metadaten"]["backup_id"] == manifest["backup_id"]
    assert ergebnis["verschluesselt"] is True
    with pytest.raises(HTTPException) as fehler:
        asyncio.run(main.backup_inspect(datei=_upload("b.zip.enc", verschluesselt), passwort="falsch"))
    assert fehler.value.status_code == 400


def test_inspect_meldet_altes_backup_ohne_manifest_nur_als_warnung(profil, tmp_path):
    inhalt = _zip_mit({"rechnungsfee.db": _db_bytes(tmp_path), "uploads/a.txt": b"x"})

    ergebnis = asyncio.run(main.backup_inspect(datei=_upload("alt.zip", inhalt), passwort=""))

    assert ergebnis["metadaten"] is None
    assert ergebnis["blockaden"] == []
    assert [w["code"] for w in ergebnis["warnungen"]] == ["manifest_fehlt"]


def test_inspect_warnt_vor_anderem_profil_und_neuerem_backup(profil):
    inhalt, _ = backup_zip.erstelle_backup_zip(profil / "rechnungsfee.db", profil, "manual")
    bm.setze_profile_id(profil, "anderes-profil")
    _schreibe_verlauf(profil, _eintrag(completed_at="2099-01-01T00:00:00Z"))

    ergebnis = asyncio.run(main.backup_inspect(datei=_upload("backup.zip", inhalt), passwort=""))

    codes = [w["code"] for w in ergebnis["warnungen"]]
    assert "anderes_profil" in codes
    assert "neueres_backup_bekannt" in codes


def test_inspect_blockiert_archiv_ohne_sqlite_datenbank(profil):
    inhalt = _zip_mit({"database/manifest.json": b"{}", "uploads/a.txt": b"x"})

    ergebnis = asyncio.run(main.backup_inspect(datei=_upload("x.zip", inhalt), passwort=""))

    assert [b["code"] for b in ergebnis["blockaden"]] == ["keine_datenbank"]


@pytest.mark.parametrize("schema, code", [(main.SCHEMA_VERSION + 1, "schema_zu_neu")])
def test_wiederherstellen_lehnt_blockaden_mit_400_ab(profil, tmp_path, schema, code):
    inhalt = _zip_mit({"rechnungsfee.db": _db_bytes(tmp_path, schema), "uploads/a.txt": b"x"})

    with pytest.raises(HTTPException) as fehler:
        asyncio.run(main.backup_wiederherstellen(datei=_upload("x.zip", inhalt), passwort=""))

    assert fehler.value.status_code == 400
    assert not main.RESTORE_MARKER_ZIP.exists()


def test_wiederherstellen_lehnt_veraenderte_datenbank_ab(profil, tmp_path):
    inhalt, _ = backup_zip.erstelle_backup_zip(profil / "rechnungsfee.db", profil, "manual")
    with zipfile.ZipFile(io.BytesIO(inhalt)) as zf:
        manifest = zf.read(bm.MANIFEST_NAME)
    veraendert = _zip_mit({"rechnungsfee.db": _db_bytes(tmp_path) + b"x", bm.MANIFEST_NAME: manifest})

    with pytest.raises(HTTPException) as fehler:
        asyncio.run(main.backup_wiederherstellen(datei=_upload("x.zip", veraendert), passwort=""))

    assert fehler.value.status_code == 400
    assert not main.RESTORE_MARKER_ZIP.exists()


def test_lokaler_restore_lehnt_veraenderten_snapshot_ab(profil):
    main._backup_datenbank("exit")
    datei = next((profil / "backups").glob("rechnungsfee_*.db"))
    datei.write_bytes(datei.read_bytes() + b"x")

    with pytest.raises(HTTPException) as fehler:
        backup_api.backup_wiederherstellen_lokal(backup_api.LokalWiederherstellenBody(dateiname=datei.name))

    assert fehler.value.status_code == 400
    assert not (profil / "restore_pending.db").exists()


def test_lokaler_restore_lehnt_neueres_schema_ab(profil):
    (profil / "backups").mkdir()
    datei = profil / "backups" / "rechnungsfee_20261101_100000.db"
    _lege_db_an(datei, main.SCHEMA_VERSION + 1)

    with pytest.raises(HTTPException) as fehler:
        backup_api.backup_wiederherstellen_lokal(backup_api.LokalWiederherstellenBody(dateiname=datei.name))

    assert fehler.value.status_code == 400


def test_lokaler_restore_funktioniert_ohne_verlaufseintrag(profil):
    (profil / "backups").mkdir()
    datei = profil / "backups" / "rechnungsfee_20261101_100000.db"
    _lege_db_an(datei)

    ergebnis = backup_api.backup_wiederherstellen_lokal(
        backup_api.LokalWiederherstellenBody(dateiname=datei.name))

    assert ergebnis["ok"] is True
    assert (profil / "restore_pending.db").exists()


# ── Einspielen beim Start ─────────────────────────────────────────────────────

def test_restore_uebernimmt_profilkennung_und_notiert_sicherheitsbackup(profil):
    bm.setze_profile_id(profil, "aus-dem-backup")
    inhalt, _ = backup_zip.erstelle_backup_zip(profil / "rechnungsfee.db", profil, "manual")
    bm.setze_profile_id(profil, "aktuelles-profil")
    main.RESTORE_MARKER_ZIP.write_bytes(inhalt)

    main._prüfe_wiederherstellung()

    assert bm.lies_profile_id(profil) == "aus-dem-backup"
    assert not main.RESTORE_MARKER_ZIP.exists()
    assert [e["backup_type"] for e in _verlauf(profil)] == ["pre_restore"]


@pytest.mark.parametrize("name", ["uploads/../../escape.txt", "uploads/C:/escape.txt", "uploads/ordner/C:/escape.txt"])
def test_restore_spielt_unsicheren_pfad_nicht_ein(profil, tmp_path, name):
    (profil / "uploads").mkdir()
    (profil / "uploads" / "bleibt.pdf").write_bytes(b"pdf")
    vorher = (profil / "rechnungsfee.db").read_bytes()
    main.RESTORE_MARKER_ZIP.write_bytes(_zip_mit({"rechnungsfee.db": _db_bytes(tmp_path, 1), name: b"nope"}))

    main._prüfe_wiederherstellung()

    assert (profil / "uploads" / "bleibt.pdf").exists()
    assert (profil / "rechnungsfee.db").read_bytes() == vorher
    assert not main.RESTORE_MARKER_ZIP.exists()
