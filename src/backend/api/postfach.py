"""
Rechnungspostfach per IMAP (Issue #451, Stufe 1).

Abruf: PDF-/XML-Anhänge aus dem eingestellten Ordner landen in der Importliste
(postfach_eingang); die verarbeitete Mail wird in den Unterordner "RechnungsFee/verarbeitet"
verschoben. Mails ohne passenden Anhang bleiben unberührt (BODY.PEEK - auch der Gelesen-Status
ändert sich nicht).

Übernahme: bewusst KEINE automatische Erfassung - das Frontend holt die Datei und startet damit
den unveränderten Einzelimport (Analyse → Formular → Prüfung → Speichern → Beleg). Danach wird
der Eintrag per /uebernommen als erledigt markiert.

Sicherheit: Das Postfach ist von außen beschreibbar - deshalb nur PDF/XML, Größenlimits, nie
automatische Buchungen. Zertifikats-Pinning (Trust on First Use) analog zum SMTP-Versand (#336).
"""
from __future__ import annotations

import email
import email.policy
import hashlib
import imaplib
import re
import socket
import ssl
import uuid
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database.connection import APP_DATA_DIR, get_db
from database.models import Beleg, PostfachEingang, Rechnung, Unternehmen

router = APIRouter(prefix="/api/postfach", tags=["Postfach"])

UPLOADS = APP_DATA_DIR / "uploads"
ZIEL_TEILE = ("RechnungsFee", "verarbeitet")
MAX_MAILS_PRO_ABRUF = 50
MAX_MAIL_BYTES = 40 * 1024 * 1024
MAX_ANHANG_BYTES = 25 * 1024 * 1024
_ENDUNGEN = {".pdf": "application/pdf", ".xml": "application/xml"}
_MIME_XML = {"application/xml", "text/xml"}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class PostfachEintrag(BaseModel):
    id: int
    original_name: str
    mime_type: str
    dateigroesse: int
    absender: Optional[str] = None
    betreff: Optional[str] = None
    mail_datum: Optional[datetime] = None
    status: str
    rechnung_id: Optional[int] = None
    erstellt_am: Optional[datetime] = None

    model_config = {"from_attributes": True}


class AbrufErgebnis(BaseModel):
    mails_geprueft: int
    mails_verschoben: int
    neu: int
    dubletten: int
    uebersprungen: list[str] = []   # Hinweise zu Mails, die nicht verarbeitet werden konnten
    weitere_mails: bool = False     # mehr als MAX_MAILS_PRO_ABRUF im Ordner


class TestErgebnis(BaseModel):
    ok: bool
    ordner: str
    anzahl_mails: int


class UebernommenRequest(BaseModel):
    rechnung_id: Optional[int] = None


# ---------------------------------------------------------------------------
# IMAP-Verbindung
# ---------------------------------------------------------------------------

def _einstellungen(db: Session) -> Unternehmen:
    u = db.query(Unternehmen).first()
    if not u or not u.imap_host or not u.imap_user or not u.imap_passwort:
        raise HTTPException(400, "Rechnungspostfach ist nicht vollständig eingerichtet (Server, Benutzer, Passwort).")
    return u


def _pruefe_gepinntes_zertifikat(u: Unternehmen, sock, db: Session) -> None:
    """Trust on First Use wie beim SMTP-Versand (api/mail.py, Issue #336)."""
    fp = hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest()
    if not u.imap_zertifikat_fingerprint:
        u.imap_zertifikat_fingerprint = fp
        db.commit()
    elif fp != u.imap_zertifikat_fingerprint:
        raise HTTPException(
            400,
            "Das Zertifikat des IMAP-Servers hat sich geändert! Das kann eine legitime "
            "Zertifikatserneuerung sein - oder ein Angriff auf die Verbindung. Zur Sicherheit wurde "
            "abgebrochen, bevor Zugangsdaten gesendet wurden. Bitte in den Postfach-Einstellungen das "
            "gemerkte Zertifikat bewusst zurücksetzen (nur wenn die Änderung erwartet ist).",
        )


def _verbinde(u: Unternehmen, db: Session) -> imaplib.IMAP4:
    ctx = ssl.create_default_context()
    if u.imap_zertifikat_ignorieren:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    port = u.imap_port or (993 if u.imap_ssl else 143)
    try:
        if u.imap_ssl:
            m = imaplib.IMAP4_SSL(u.imap_host, port, ssl_context=ctx, timeout=20)
        else:
            m = imaplib.IMAP4(u.imap_host, port, timeout=20)
            m.starttls(ssl_context=ctx)
        if u.imap_zertifikat_ignorieren:
            _pruefe_gepinntes_zertifikat(u, m.sock, db)
        m.login(u.imap_user, u.imap_passwort)
        return m
    except HTTPException:
        raise
    except imaplib.IMAP4.error as e:
        raise HTTPException(400, f"Anmeldung am Postfach fehlgeschlagen – Benutzer oder Passwort prüfen ({_text(e)}).")
    except ssl.SSLCertVerificationError as e:
        grund = "ein selbstsigniertes" if "self-signed certificate" in str(e) else "ein nicht vertrauenswürdiges"
        raise HTTPException(
            400,
            f'Zertifikatsprüfung fehlgeschlagen: Der IMAP-Server „{u.imap_host}" verwendet {grund} Zertifikat. '
            "Ist der Server vertrauenswürdig (z. B. eigener Mailserver), kannst du in den Einstellungen "
            "„Selbstsigniertes Zertifikat akzeptieren“ aktivieren.",
        )
    except ssl.SSLError as e:
        raise HTTPException(400, f"TLS/SSL-Fehler bei der Verbindung zu {u.imap_host}:{port}: {e}")
    except socket.gaierror:
        raise HTTPException(400, f'IMAP-Server „{u.imap_host}" wurde nicht gefunden – Servername und Internetverbindung prüfen.')
    except (TimeoutError, socket.timeout):
        raise HTTPException(400, f"Zeitüberschreitung bei der Verbindung zu {u.imap_host}:{port} – Servername, Port und Firewall prüfen.")
    except ConnectionRefusedError:
        raise HTTPException(400, f"Verbindung zu {u.imap_host}:{port} abgelehnt – Port und SSL-Einstellung prüfen (993 mit SSL, 143 mit STARTTLS).")
    except OSError as e:
        raise HTTPException(400, f"Netzwerkfehler: {e}")


def _text(wert) -> str:
    if isinstance(wert, bytes):
        return wert.decode("utf-8", "replace")
    if isinstance(wert, (list, tuple)) and wert:
        return _text(wert[0])
    return str(wert)


def _q(name: str) -> str:
    """Ordnernamen für IMAP-Befehle quoten (Leerzeichen, Sonderzeichen)."""
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _trennzeichen(m: imaplib.IMAP4) -> str:
    typ, daten = m.list('""', '""')
    if typ == "OK" and daten and daten[0]:
        treffer = re.search(rb'\(.*?\)\s+"(.)"', daten[0] if isinstance(daten[0], bytes) else b"")
        if treffer:
            return treffer.group(1).decode()
    return "/"


def _zielordner_anlegen(m: imaplib.IMAP4) -> str:
    trenner = _trennzeichen(m)
    pfad = ""
    for teil in ZIEL_TEILE:
        pfad = f"{pfad}{trenner}{teil}" if pfad else teil
        m.create(_q(pfad))       # existiert er schon, antwortet der Server mit NO - unkritisch
        m.subscribe(_q(pfad))    # im Mailprogramm sichtbar machen
    return pfad


def _hat_faehigkeit(m: imaplib.IMAP4, name: str) -> bool:
    return name.upper() in {c.upper() for c in (m.capabilities or ())}


def _verschiebe(m: imaplib.IMAP4, uid: bytes, ziel: str) -> None:
    if _hat_faehigkeit(m, "MOVE"):
        typ, daten = m.uid("MOVE", uid, _q(ziel))
        if typ != "OK":
            raise RuntimeError(_text(daten))
        return
    typ, daten = m.uid("COPY", uid, _q(ziel))
    if typ != "OK":
        raise RuntimeError(_text(daten))
    m.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
    # Nur die eigene Mail endgültig entfernen - ein ordnerweites EXPUNGE würde auch Mails löschen,
    # die der Nutzer selbst nur als gelöscht markiert hat. Ohne UIDPLUS bleibt sie markiert liegen.
    if _hat_faehigkeit(m, "UIDPLUS"):
        m.uid("EXPUNGE", uid)


# ---------------------------------------------------------------------------
# Mail-Auswertung
# ---------------------------------------------------------------------------

def _anhaenge(msg) -> list[tuple[str, str, bytes]]:
    """(Dateiname, MIME-Typ, Inhalt) aller PDF-/XML-Anhänge."""
    ergebnis = []
    for teil in msg.walk():
        if teil.is_multipart():
            continue
        name = teil.get_filename() or ""
        mime = (teil.get_content_type() or "").lower()
        endung = Path(name).suffix.lower()
        if endung in _ENDUNGEN:
            mime = _ENDUNGEN[endung]
        elif mime == "application/pdf":
            name = name or "rechnung.pdf"
        elif mime in _MIME_XML and teil.get_content_disposition() == "attachment":
            name, mime = name or "rechnung.xml", "application/xml"
        else:
            continue
        inhalt = teil.get_payload(decode=True) or b""
        if not inhalt:
            continue
        ergebnis.append((name, mime, inhalt))
    return ergebnis


def _dateiname_sicher(name: str) -> str:
    stamm = re.sub(r"[^\w.\-]+", "_", Path(name).stem)[:60] or "anhang"
    return f"{stamm}_{uuid.uuid4().hex[:8]}{Path(name).suffix.lower()}"


@router.post("/test", response_model=TestErgebnis)
def postfach_testen(db: Session = Depends(get_db)):
    """Verbindung, Anmeldung und Ordner prüfen - ändert nichts im Postfach."""
    u = _einstellungen(db)
    m = _verbinde(u, db)
    try:
        typ, daten = m.select(_q(u.imap_ordner or "INBOX"), readonly=True)
        if typ != "OK":
            raise HTTPException(400, f'Ordner „{u.imap_ordner}" nicht gefunden.')
        return TestErgebnis(ok=True, ordner=u.imap_ordner or "INBOX", anzahl_mails=int(_text(daten) or 0))
    finally:
        try:
            m.logout()
        except Exception:
            pass


@router.post("/abrufen", response_model=AbrufErgebnis)
def postfach_abrufen(db: Session = Depends(get_db)):
    u = _einstellungen(db)
    m = _verbinde(u, db)
    ergebnis = AbrufErgebnis(mails_geprueft=0, mails_verschoben=0, neu=0, dubletten=0)
    try:
        typ, _ = m.select(_q(u.imap_ordner or "INBOX"))
        if typ != "OK":
            raise HTTPException(400, f'Ordner „{u.imap_ordner}" nicht gefunden.')
        typ, daten = m.uid("SEARCH", None, "ALL")
        uids = (daten[0] or b"").split() if typ == "OK" and daten else []
        if len(uids) > MAX_MAILS_PRO_ABRUF:
            ergebnis.weitere_mails = True
            uids = uids[:MAX_MAILS_PRO_ABRUF]   # älteste zuerst; Rest beim nächsten Abruf
        ziel: Optional[str] = None

        for uid in uids:
            ergebnis.mails_geprueft += 1
            typ, groesse_daten = m.uid("FETCH", uid, "(RFC822.SIZE)")
            groesse = re.search(rb"RFC822\.SIZE (\d+)", groesse_daten[0] if groesse_daten and isinstance(groesse_daten[0], bytes) else b"")
            if groesse and int(groesse.group(1)) > MAX_MAIL_BYTES:
                ergebnis.uebersprungen.append(f"Mail UID {_text(uid)} ist größer als {MAX_MAIL_BYTES // 1024 // 1024} MB – übersprungen.")
                continue
            typ, roh = m.uid("FETCH", uid, "(BODY.PEEK[])")
            mail_bytes = next((t[1] for t in (roh or []) if isinstance(t, tuple) and len(t) > 1), None)
            if typ != "OK" or not mail_bytes:
                continue
            msg = email.message_from_bytes(mail_bytes, policy=email.policy.default)
            anhaenge = _anhaenge(msg)
            if not anhaenge:
                continue   # Mails ohne Rechnungsanhang bleiben unberührt

            absender = str(msg.get("From", ""))[:300] or None
            betreff = str(msg.get("Subject", ""))[:500] or None
            try:
                mail_datum = parsedate_to_datetime(msg.get("Date")) if msg.get("Date") else None
                if mail_datum and mail_datum.tzinfo:
                    mail_datum = mail_datum.astimezone().replace(tzinfo=None)
            except (TypeError, ValueError):
                mail_datum = None

            for name, mime, inhalt in anhaenge:
                if len(inhalt) > MAX_ANHANG_BYTES:
                    ergebnis.uebersprungen.append(f'Anhang „{name}" ({betreff or "ohne Betreff"}) ist zu groß – übersprungen.')
                    continue
                sha = hashlib.sha256(inhalt).hexdigest()
                if (db.query(PostfachEingang.id).filter(PostfachEingang.sha256 == sha).first()
                        or db.query(Beleg.id).filter(Beleg.sha256 == sha).first()):
                    ergebnis.dubletten += 1
                    continue
                jetzt = datetime.now()
                rel = Path("postfach") / str(jetzt.year) / jetzt.strftime("%m") / _dateiname_sicher(name)
                (UPLOADS / rel).parent.mkdir(parents=True, exist_ok=True)
                (UPLOADS / rel).write_bytes(inhalt)
                db.add(PostfachEingang(
                    sha256=sha, datei_pfad=rel.as_posix(), original_name=name[:255], mime_type=mime,
                    dateigroesse=len(inhalt), absender=absender, betreff=betreff, mail_datum=mail_datum,
                ))
                db.commit()
                ergebnis.neu += 1

            # Erst verschieben, wenn alle Anhänge gesichert sind
            if ziel is None:
                ziel = _zielordner_anlegen(m)
            try:
                _verschiebe(m, uid, ziel)
                ergebnis.mails_verschoben += 1
            except Exception as e:
                ergebnis.uebersprungen.append(
                    f'Mail „{betreff or "ohne Betreff"}" wurde übernommen, konnte aber nicht verschoben werden: {e}'
                )
        return ergebnis
    finally:
        try:
            m.logout()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Importliste
# ---------------------------------------------------------------------------

def _eintrag(eintrag_id: int, db: Session) -> PostfachEingang:
    e = db.query(PostfachEingang).filter(PostfachEingang.id == eintrag_id).first()
    if not e:
        raise HTTPException(404, "Eintrag nicht gefunden.")
    return e


@router.get("/eingang", response_model=list[PostfachEintrag])
def postfach_eingang(status: str = "offen", db: Session = Depends(get_db)):
    q = db.query(PostfachEingang)
    if status != "alle":
        q = q.filter(PostfachEingang.status == status)
    return q.order_by(PostfachEingang.mail_datum.desc(), PostfachEingang.id.desc()).all()


@router.get("/eingang/{eintrag_id}/datei")
def postfach_datei(eintrag_id: int, db: Session = Depends(get_db)):
    e = _eintrag(eintrag_id, db)
    if not e.datei_pfad or not (UPLOADS / e.datei_pfad).exists():
        raise HTTPException(404, "Datei nicht mehr vorhanden.")
    # inline: PDFs werden per openUrl() im WebviewWindow angezeigt (CLAUDE.md)
    return FileResponse(
        UPLOADS / e.datei_pfad, media_type=e.mime_type,
        headers={"Content-Disposition": f'inline; filename="{Path(e.datei_pfad).name}"'},
    )


@router.post("/eingang/{eintrag_id}/uebernommen", response_model=PostfachEintrag)
def postfach_uebernommen(eintrag_id: int, body: UebernommenRequest, db: Session = Depends(get_db)):
    """Nach erfolgreichem Einzelimport: Eintrag erledigt. Die Datei hängt dann als Beleg an der
    Rechnung - die Postfach-Kopie wird nicht mehr gebraucht."""
    e = _eintrag(eintrag_id, db)
    if body.rechnung_id is not None and not db.query(Rechnung.id).filter(Rechnung.id == body.rechnung_id).first():
        raise HTTPException(404, "Rechnung nicht gefunden.")
    e.status = "uebernommen"
    e.rechnung_id = body.rechnung_id
    _datei_entfernen(e)
    db.commit()
    db.refresh(e)
    return e


@router.post("/eingang/{eintrag_id}/verwerfen", response_model=PostfachEintrag)
def postfach_verwerfen(eintrag_id: int, db: Session = Depends(get_db)):
    """Kein Rechnungsbeleg (z. B. AGB, Werbung). Zeile bleibt als Dublettenschutz bestehen."""
    e = _eintrag(eintrag_id, db)
    e.status = "verworfen"
    _datei_entfernen(e)
    db.commit()
    db.refresh(e)
    return e


def _datei_entfernen(e: PostfachEingang) -> None:
    if e.datei_pfad:
        pfad = UPLOADS / e.datei_pfad
        if pfad.exists():
            pfad.unlink()
        e.datei_pfad = None
