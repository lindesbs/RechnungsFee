"""Metadaten für Backups: Manifest, Verlauf, Profilkennung und Archivprüfung.

Das Modul greift bewusst weder auf die Datenbank noch auf die API zu. Alles, was es
braucht, bekommt es als Parameter (siehe docs/superpowers/specs/
2026-11-07-backup-metadaten-design.md).
"""
import hashlib
import json
import os
import re
import tempfile
import uuid
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1
MAX_MANIFEST_BYTES = 1024 * 1024
PROFILE_ID_DATEI = "profil_id.txt"
VERLAUF_DATEI = "backup-verlauf.jsonl"
MAX_VERLAUF_EINTRAEGE = 500
BACKUP_TYPEN = frozenset({"manual", "exit", "pre_migration", "pre_restore"})
SICHERHEITS_TYPEN = frozenset({"pre_migration", "pre_restore"})

_SQLITE_DATEI = "rechnungsfee.db"
_PORTABLES_MANIFEST = "database/manifest.json"
_SQLITE_KENNUNG = b"SQLite format 3\x00"
_ZIP_LESEFEHLER = (KeyError, zipfile.BadZipFile, zlib.error, EOFError)


def schreibe_atomar(ziel: Path, daten: bytes) -> None:
    """Schreibt über eine temporäre Datei im Zielordner: Was unter dem Zielnamen
    existiert, ist immer vollständig."""
    fd, temp_name = tempfile.mkstemp(dir=ziel.parent, prefix=f".{ziel.name}.")
    try:
        with os.fdopen(fd, "wb") as datei:
            datei.write(daten)
            datei.flush()
            os.fsync(datei.fileno())
        os.replace(temp_name, ziel)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def setze_profile_id(profil_dir: Path, profile_id: str) -> None:
    schreibe_atomar(profil_dir / PROFILE_ID_DATEI, profile_id.encode("utf-8"))


def lies_profile_id(profil_dir: Path) -> str | None:
    """Legt nichts an: Die Prüfung eines Backups darf keine Spuren hinterlassen."""
    try:
        return (profil_dir / PROFILE_ID_DATEI).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def lade_oder_erzeuge_profile_id(profil_dir: Path) -> str:
    vorhanden = lies_profile_id(profil_dir)
    if vorhanden:
        return vorhanden
    neu = str(uuid.uuid4())
    setze_profile_id(profil_dir, neu)
    return neu


def erstelle_manifest(*, backup_type: str, app_version: str, schema_version: int,
                      profile_id: str, profile_name: str, created_at: str,
                      completed_at: str, contents: dict, integrity_files: dict) -> dict:
    return {
        "manifest_version": MANIFEST_VERSION,
        "application": "RechnungsFee",
        "backup_id": str(uuid.uuid4()),
        "app_version": app_version,
        "schema_version": schema_version,
        "profile_id": profile_id,
        "profile_name": profile_name,
        "backup_type": backup_type,
        "created_at": created_at,
        "completed_at": completed_at,
        "contents": contents,
        "integrity": {"algorithm": "sha256", "files": integrity_files},
    }


def parse_zeitpunkt(wert) -> datetime | None:
    """ISO-8601 mit Zeitzone. Ohne Zeitzone wäre ein Vergleich zwischen Rechnern
    nicht eindeutig, deshalb gilt das als ungültig."""
    if not isinstance(wert, str):
        return None
    try:
        zeitpunkt = datetime.fromisoformat(wert.replace("Z", "+00:00"))
    except ValueError:
        return None
    return zeitpunkt if zeitpunkt.tzinfo is not None else None


def _ist_ganzzahl(wert) -> bool:
    return isinstance(wert, int) and not isinstance(wert, bool)


def manifest_fehler(daten) -> list[str]:
    """Liefert alle Gründe, aus denen ein Manifest ungültig ist. Leer heißt gültig."""
    if not isinstance(daten, dict):
        return ["Manifest ist kein Objekt"]

    fehler = []
    if not _ist_ganzzahl(daten.get("manifest_version")):
        fehler.append("manifest_version fehlt")
    for feld in ("backup_id", "app_version"):
        if not isinstance(daten.get(feld), str) or not daten[feld]:
            fehler.append(f"{feld} fehlt")
    if not _ist_ganzzahl(daten.get("schema_version")):
        fehler.append("schema_version ist keine Zahl")
    if daten.get("backup_type") not in BACKUP_TYPEN:
        fehler.append("backup_type ist unbekannt")
    for feld in ("created_at", "completed_at"):
        if parse_zeitpunkt(daten.get(feld)) is None:
            fehler.append(f"{feld} ist kein Zeitpunkt mit Zeitzone")
    if not isinstance(daten.get("contents"), dict):
        fehler.append("contents fehlt")
    integrity = daten.get("integrity")
    if (not isinstance(integrity, dict) or integrity.get("algorithm") != "sha256"
            or not isinstance(integrity.get("files"), dict)):
        fehler.append("integrity ist ungültig")
    return fehler


def lies_verlauf(pfad: Path) -> list[dict]:
    """Liest den Verlauf. Kaputte Zeilen werden übersprungen: Ein beschädigter Eintrag
    darf die Backup-Übersicht nicht unbrauchbar machen."""
    try:
        zeilen = pfad.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    eintraege = []
    for zeile in zeilen:
        try:
            eintrag = json.loads(zeile)
        except ValueError:
            continue
        if isinstance(eintrag, dict):
            eintraege.append(eintrag)
    return eintraege


def verlauf_anhaengen(pfad: Path, eintrag: dict,
                      max_eintraege: int = MAX_VERLAUF_EINTRAEGE) -> None:
    pfad.parent.mkdir(parents=True, exist_ok=True)
    eintraege = [*lies_verlauf(pfad), eintrag][-max_eintraege:]
    inhalt = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in eintraege)
    schreibe_atomar(pfad, inhalt.encode("utf-8"))


def verlauf_eintrag(manifest: dict, *, dateiname: str, ort: str, ziel: str | None,
                    umfang: str, groesse: int) -> dict:
    return {
        "backup_id": manifest["backup_id"],
        "dateiname": dateiname,
        "ort": ort,
        "ziel": ziel,
        "backup_type": manifest["backup_type"],
        "umfang": umfang,
        "created_at": manifest["created_at"],
        "completed_at": manifest["completed_at"],
        "app_version": manifest["app_version"],
        "schema_version": manifest["schema_version"],
        "profile_id": manifest["profile_id"],
        "groesse": groesse,
        "integrity": manifest["integrity"],
    }


def _sortierschluessel(eintrag: dict) -> tuple:
    zeitpunkt = parse_zeitpunkt(eintrag.get("completed_at"))
    return (
        zeitpunkt is not None,
        zeitpunkt.timestamp() if zeitpunkt is not None else 0.0,
        str(eintrag.get("backup_id") or ""),
    )


def sortiere_neueste_zuerst(eintraege: list[dict]) -> list[dict]:
    return sorted(eintraege, key=_sortierschluessel, reverse=True)


def neueste_vollsicherung(eintraege: list[dict]) -> dict | None:
    """Sicherheitsbackups zählen nicht: Nach dem Einspielen eines alten Backups wäre
    sonst der gerade verworfene Stand die „neueste“ Sicherung."""
    kandidaten = [
        e for e in eintraege
        if e.get("umfang") == "voll"
        and e.get("backup_type") not in SICHERHEITS_TYPEN
        and parse_zeitpunkt(e.get("completed_at")) is not None
    ]
    return sortiere_neueste_zuerst(kandidaten)[0] if kandidaten else None


@dataclass
class Pruefergebnis:
    manifest: dict | None = None
    blockaden: list[dict] = field(default_factory=list)
    warnungen: list[dict] = field(default_factory=list)
    infos: list[dict] = field(default_factory=list)


def _hinweis(code: str, meldung: str) -> dict:
    return {"code": code, "meldung": meldung}


def _lies_json(zf: zipfile.ZipFile, name: str):
    """Liefert None, wenn der Eintrag fehlt, zu groß oder kein JSON ist."""
    try:
        with zf.open(name) as datei:
            roh = datei.read(MAX_MANIFEST_BYTES + 1)
        if len(roh) > MAX_MANIFEST_BYTES:
            return None
        return json.loads(roh)
    except (*_ZIP_LESEFEHLER, ValueError, RecursionError):
        return None


def _lies_sqlite_schema(zf: zipfile.ZipFile) -> int | None:
    """user_version steht ab Byte 60 im SQLite-Header. So lässt sich das Schema lesen,
    ohne die Datenbank zu entpacken."""
    try:
        with zf.open(_SQLITE_DATEI) as datei:
            kopf = datei.read(100)
    except _ZIP_LESEFEHLER:
        return None
    if len(kopf) < 100 or not kopf.startswith(_SQLITE_KENNUNG):
        return None
    return int.from_bytes(kopf[60:64], "big")


def _lies_schema_ohne_manifest(zf: zipfile.ZipFile, namen: set[str]) -> int | None:
    if _SQLITE_DATEI in namen:
        return _lies_sqlite_schema(zf)
    daten = _lies_json(zf, _PORTABLES_MANIFEST)
    version = daten.get("schema_version") if isinstance(daten, dict) else None
    return version if _ist_ganzzahl(version) else None


def _ist_unsicherer_upload_pfad(name: str) -> bool:
    # Der Restore normalisiert "\" zu "/", die Prüfung muss dasselbe sehen wie er.
    relativ = PurePosixPath(name[len("uploads/"):].replace("\\", "/"))
    if relativ.is_absolute() or ".." in relativ.parts:
        return True
    # Jeder Teil zählt: joinpath springt unter Windows auch bei ordner/C:/datei aufs Laufwerk.
    return any(re.match(r"^[A-Za-z]:", teil) for teil in relativ.parts)


def _lies_root_manifest(zf: zipfile.ZipFile, namen: set[str], ergebnis: Pruefergebnis) -> dict | None:
    if MANIFEST_NAME not in namen:
        ergebnis.warnungen.append(_hinweis(
            "manifest_fehlt", "Metadaten nicht vorhanden. Das Backup stammt vermutlich aus einer älteren Version."))
        return None
    daten = _lies_json(zf, MANIFEST_NAME)
    version = daten.get("manifest_version") if isinstance(daten, dict) else None
    if _ist_ganzzahl(version) and version > MANIFEST_VERSION:
        ergebnis.blockaden.append(_hinweis(
            "manifest_zu_neu", "Dieses Backup stammt aus einer neueren Version. Bitte aktualisiere RechnungsFee zuerst."))
        return None
    if manifest_fehler(daten):
        ergebnis.warnungen.append(_hinweis(
            "manifest_defekt", "Die Metadaten sind beschädigt, die Datenbank konnte deshalb nicht geprüft werden."))
        return None
    return daten


def _pruefe_hashes(zf: zipfile.ZipFile, manifest: dict, ergebnis: Pruefergebnis) -> None:
    for name, erwartet in manifest["integrity"]["files"].items():
        try:
            with zf.open(name) as datei:
                aktuell = hashlib.file_digest(datei, "sha256").hexdigest()
        except _ZIP_LESEFEHLER:
            aktuell = None
        if aktuell != erwartet:
            ergebnis.blockaden.append(_hinweis(
                "hash_abweichung", f"{name} ist beschädigt oder wurde verändert."))


def _pruefe_schema(version: int | None, aktuell: int, ergebnis: Pruefergebnis) -> None:
    if version is None:
        ergebnis.warnungen.append(_hinweis(
            "schema_unbekannt", "Die Datenbankversion dieses Backups konnte nicht ermittelt werden."))
    elif version > aktuell:
        ergebnis.blockaden.append(_hinweis(
            "schema_zu_neu", "Dieses Backup stammt aus einer neueren Version. Bitte aktualisiere RechnungsFee zuerst."))
    elif version < aktuell:
        ergebnis.infos.append(_hinweis(
            "schema_aelter", "Das Backup stammt aus einer älteren Version und wird beim Start aktualisiert."))


def pruefe_archiv(zf: zipfile.ZipFile, *, schema_version: int, profile_id: str | None = None,
                  neuester_zeitpunkt: str | None = None) -> Pruefergebnis:
    """Prüft ein Backup-ZIP, ohne etwas zu schreiben.

    Blockaden verhindern den Restore. Warnungen muss die Oberfläche bestätigen lassen.
    Ein defektes Manifest blockiert bewusst nicht: Die Datenbank kann intakt sein, und
    ein Totalverlust ist der schlechteste Moment für eine Sperre.
    """
    ergebnis = Pruefergebnis()
    namen = set(zf.namelist())
    uploads = [n for n in namen if n.startswith("uploads/") and not n.endswith("/")]

    if any(_ist_unsicherer_upload_pfad(n) for n in uploads):
        ergebnis.blockaden.append(_hinweis(
            "unsicherer_pfad", "Das Backup enthält einen ungültigen Dateipfad und wird nicht eingespielt."))
    if _SQLITE_DATEI not in namen and _PORTABLES_MANIFEST not in namen:
        ergebnis.blockaden.append(_hinweis("keine_datenbank", "Das Backup enthält keine Datenbank."))

    manifest = _lies_root_manifest(zf, namen, ergebnis)
    ergebnis.manifest = manifest
    if manifest is not None:
        _pruefe_hashes(zf, manifest, ergebnis)
        _pruefe_schema(manifest["schema_version"], schema_version, ergebnis)
        fremd = manifest.get("profile_id")
        if profile_id and fremd and fremd != profile_id:
            ergebnis.warnungen.append(_hinweis(
                "anderes_profil", "Dieses Backup stammt aus einem anderen Profil."))
        neuester = parse_zeitpunkt(neuester_zeitpunkt)
        if neuester is not None and parse_zeitpunkt(manifest["completed_at"]) < neuester:
            ergebnis.warnungen.append(_hinweis(
                "neueres_backup_bekannt", "Es gibt ein neueres Backup als dieses."))
    elif not any(b["code"] == "manifest_zu_neu" for b in ergebnis.blockaden):
        _pruefe_schema(_lies_schema_ohne_manifest(zf, namen), schema_version, ergebnis)

    if not uploads:
        ergebnis.warnungen.append(_hinweis(
            "nur_datenbank", "Das Backup enthält keine Belege. Deine vorhandenen Belege bleiben unverändert."))
    return ergebnis
