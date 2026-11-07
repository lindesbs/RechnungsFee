"""Erstellt Backups samt Metadaten und trägt sie in den Verlauf ein.

Gemeinsame Grundlage für Download und externe Ziele. Anders als utils/backup_metadata.py
liest dieses Modul die SQLite-Datei. Der Schema-Stand kommt aus der Datei selbst, weil
SCHEMA_VERSION in main.py liegt und api/backup.py main nicht importieren kann.
"""
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from utils import backup_metadata as bm


def app_version() -> str:
    """Der Build schreibt _app_version.py vor dem Sidecar-Bau, lokal gibt es die Datei nicht."""
    try:
        from _app_version import APP_VERSION
    except ImportError:
        return "dev"
    return APP_VERSION


def jetzt_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_datei(pfad: Path) -> str:
    summe = hashlib.sha256()
    with pfad.open("rb") as datei:
        for block in iter(lambda: datei.read(1024 * 1024), b""):
            summe.update(block)
    return summe.hexdigest()


def lies_schema_version(db_path: Path) -> int | None:
    """None, wenn die Datei keine lesbare SQLite-Datenbank ist."""
    try:
        # Bewusst nicht mode=ro: WAL-Dateien lassen sich so ohne -shm nicht zuverlässig öffnen.
        verbindung = sqlite3.connect(str(db_path))
        try:
            return verbindung.execute("PRAGMA user_version").fetchone()[0]
        finally:
            verbindung.close()
    except sqlite3.Error:
        return None


def _manifest(*, backup_type: str, profil_dir: Path, schema_version: int, created_at: str,
              contents: dict, integrity_files: dict) -> dict:
    return bm.erstelle_manifest(
        backup_type=backup_type,
        app_version=app_version(),
        schema_version=schema_version,
        profile_id=bm.lade_oder_erzeuge_profile_id(profil_dir),
        profile_name=profil_dir.name,
        created_at=created_at,
        completed_at=jetzt_utc(),
        contents=contents,
        integrity_files=integrity_files,
    )


def erstelle_backup_zip(db_path: Path, profil_dir: Path, backup_type: str) -> tuple[bytes, dict]:
    """ZIP mit WAL-sicherem DB-Snapshot und Uploads. Das Manifest ist der letzte Eintrag."""
    created_at = jetzt_utc()
    uploads_dir = profil_dir / "uploads"
    uploads = sorted(f for f in uploads_dir.rglob("*") if f.is_file()) if uploads_dir.exists() else []

    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w", zipfile.ZIP_DEFLATED) as zf:
        fd, temp_name = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        snapshot = Path(temp_name)
        try:
            quelle = sqlite3.connect(str(db_path))
            ziel = sqlite3.connect(temp_name)
            try:
                quelle.backup(ziel)
            finally:
                ziel.close()
                quelle.close()
            schema_version = lies_schema_version(snapshot)
            db_hash = sha256_datei(snapshot)
            zf.write(snapshot, "rechnungsfee.db")
        finally:
            snapshot.unlink(missing_ok=True)

        for datei in uploads:
            zf.write(datei, f"uploads/{datei.relative_to(uploads_dir).as_posix()}")

        manifest = _manifest(
            backup_type=backup_type,
            profil_dir=profil_dir,
            schema_version=schema_version,
            created_at=created_at,
            contents={"format": "sqlite", "database": True, "uploads": bool(uploads),
                      "file_count": len(uploads) + 1},
            integrity_files={"rechnungsfee.db": db_hash},
        )
        zf.writestr(bm.MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
    return puffer.getvalue(), manifest


def notiere_backup(profil_dir: Path, manifest: dict, *, dateiname: str, ort: str,
                   ziel: str | None, umfang: str, groesse: int) -> None:
    """Der Verlauf ist reine Anzeige: Ein Fehler hier darf kein Backup scheitern lassen."""
    eintrag = bm.verlauf_eintrag(manifest, dateiname=dateiname, ort=ort, ziel=ziel,
                                 umfang=umfang, groesse=groesse)
    try:
        bm.verlauf_anhaengen(profil_dir / "backups" / bm.VERLAUF_DATEI, eintrag)
    except OSError as fehler:
        print(f"[Backup] Verlauf nicht geschrieben: {fehler}")


def notiere_snapshot(snapshot: Path, profil_dir: Path, backup_type: str, created_at: str) -> None:
    """Lokale .db-Snapshots haben kein ZIP, ihre Metadaten stehen nur im Verlauf."""
    try:
        manifest = _manifest(
            backup_type=backup_type,
            profil_dir=profil_dir,
            schema_version=lies_schema_version(snapshot),
            created_at=created_at,
            contents={"format": "sqlite", "database": True, "uploads": False, "file_count": 1},
            integrity_files={snapshot.name: sha256_datei(snapshot)},
        )
    except OSError as fehler:
        print(f"[Backup] Verlauf nicht geschrieben: {fehler}")
        return
    notiere_backup(profil_dir, manifest, dateiname=snapshot.name, ort="lokal", ziel=None,
                   umfang="nur_db", groesse=snapshot.stat().st_size)
