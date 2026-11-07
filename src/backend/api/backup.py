import io
import shutil
from datetime import date, datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from database.connection import DB_PATH, APP_DATA_DIR
from utils import backup_metadata, backup_zip

router = APIRouter(prefix="/api/backup", tags=["backup"])


@router.get("/download")
def backup_download():
    """Vollständiges Backup als ZIP: WAL-sicherer DB-Snapshot + Uploads-Ordner + Manifest."""
    datum = date.today().strftime("%Y-%m-%d")
    filename = f"RechnungsFee-Backup-{datum}.zip"

    inhalt, manifest = backup_zip.erstelle_backup_zip(DB_PATH, APP_DATA_DIR, "manual")
    backup_zip.notiere_backup(APP_DATA_DIR, manifest, dateiname=filename, ort="download",
                              ziel=None, umfang="voll", groesse=len(inhalt))
    return StreamingResponse(
        io.BytesIO(inhalt),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _lies_verlauf() -> list[dict]:
    return backup_metadata.lies_verlauf(APP_DATA_DIR / "backups" / backup_metadata.VERLAUF_DATEI)


def _listeneintrag(*, dateiname: str, timestamp: str | None, groesse: int, verlauf: dict | None,
                   wiederherstellbar: bool, neueste_id: str | None) -> dict:
    daten = verlauf or {}
    return {
        "dateiname": dateiname,
        "timestamp": timestamp,
        "groesse": groesse,
        "backup_type": daten.get("backup_type"),
        "umfang": daten.get("umfang"),
        "ort": daten.get("ort", "lokal"),
        "ziel": daten.get("ziel"),
        "app_version": daten.get("app_version"),
        "schema_version": daten.get("schema_version"),
        "hat_metadaten": verlauf is not None,
        "wiederherstellbar": wiederherstellbar,
        "ist_neueste_vollsicherung": verlauf is not None and daten.get("backup_id") == neueste_id,
    }


def _sortierzeit(eintrag: dict) -> datetime:
    """Zeitstempel aus Dateinamen sind lokale Zeit, die aus dem Verlauf UTC."""
    wert = eintrag["timestamp"]
    if wert is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(wert.replace("Z", "+00:00")).astimezone()


@router.get("/liste")
def backup_liste():
    """Lokale Snapshots und Einträge aus dem Verlauf (neueste zuerst).

    Externe Backups und Downloads sind reine Anzeige: Der Verlauf belegt nicht, dass die
    Datei noch existiert. Eingespielt werden sie per Upload.
    """
    backups_dir = APP_DATA_DIR / "backups"
    if not backups_dir.exists():
        return []
    verlauf = _lies_verlauf()
    neueste = backup_metadata.neueste_vollsicherung(verlauf)
    neueste_id = neueste["backup_id"] if neueste else None
    lokal = {e.get("dateiname"): e for e in verlauf if e.get("ort") == "lokal"}

    eintraege = []
    for f in backups_dir.glob("rechnungsfee_*.db"):
        try:
            teile = f.stem.split("_")  # rechnungsfee_20260613_143022
            ts = datetime.strptime(f"{teile[1]}_{teile[2]}", "%Y%m%d_%H%M%S").isoformat() if len(teile) >= 3 else None
        except (ValueError, IndexError):
            ts = None
        eintraege.append(_listeneintrag(
            dateiname=f.name, timestamp=ts, groesse=f.stat().st_size, verlauf=lokal.get(f.name),
            wiederherstellbar=True, neueste_id=neueste_id,
        ))
    for eintrag in verlauf:
        if eintrag.get("ort") != "lokal":
            eintraege.append(_listeneintrag(
                dateiname=eintrag.get("dateiname"),
                timestamp=eintrag["completed_at"] if backup_metadata.parse_zeitpunkt(eintrag.get("completed_at")) else None,
                groesse=eintrag.get("groesse"), verlauf=eintrag,
                wiederherstellbar=False, neueste_id=neueste_id,
            ))
    eintraege.sort(key=_sortierzeit, reverse=True)
    return eintraege


class LokalWiederherstellenBody(BaseModel):
    dateiname: str


@router.post("/wiederherstellen-lokal")
def backup_wiederherstellen_lokal(body: LokalWiederherstellenBody):
    """Kopiert einen lokalen DB-Snapshot als restore_pending.db – wird beim Neustart eingespielt."""
    if not body.dateiname.startswith("rechnungsfee_") or not body.dateiname.endswith(".db"):
        raise HTTPException(400, "Ungültiger Dateiname")
    backup_pfad = APP_DATA_DIR / "backups" / body.dateiname
    if not backup_pfad.exists():
        raise HTTPException(404, "Backup nicht gefunden")
    eintrag = next((e for e in reversed(_lies_verlauf())
                    if e.get("ort") == "lokal" and e.get("dateiname") == body.dateiname), None)
    erwartet = ((eintrag or {}).get("integrity") or {}).get("files", {}).get(body.dateiname)
    if erwartet and backup_zip.sha256_datei(backup_pfad) != erwartet:
        raise HTTPException(400, "Dieses Backup wurde verändert oder ist beschädigt und wird nicht eingespielt.")
    schema = backup_zip.lies_schema_version(backup_pfad)
    aktuell = backup_zip.lies_schema_version(DB_PATH)
    if schema is not None and aktuell is not None and schema > aktuell:
        raise HTTPException(400, "Dieses Backup stammt aus einer neueren Version. Aktualisiere RechnungsFee zuerst.")
    shutil.copy2(str(backup_pfad), str(APP_DATA_DIR / "restore_pending.db"))
    return {"ok": True, "neustart_erforderlich": True}
