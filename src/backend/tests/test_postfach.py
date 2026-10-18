"""
Issue #451, Stufe 1: Rechnungspostfach per IMAP.

Abruf legt PDF-/XML-Anhänge in die Importliste und verschiebt die Mail nach
"RechnungsFee/verarbeitet"; Mails ohne Rechnungsanhang bleiben unberührt; Dubletten (bereits
in der Liste oder schon als Beleg vorhanden) werden nicht erneut übernommen. Der IMAP-Server
ist hier nachgebaut (keine Netzwerkverbindung).
"""
import re
from email.message import EmailMessage
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import api.postfach as postfach
from database.connection import Base
from database.models import Beleg, PostfachEingang, Unternehmen

PDF = b"%PDF-1.4\n% Testrechnung\n%%EOF\n"
XML = b"<?xml version='1.0'?><Invoice>Test</Invoice>"


def _mail(betreff: str, anhaenge: list[tuple[str, str, bytes]]) -> bytes:
    msg = EmailMessage()
    msg["From"] = "Lieferant <rechnung@lieferant.example>"
    msg["To"] = "rechnungen@firma.example"
    msg["Subject"] = betreff
    msg["Date"] = "Sat, 17 Oct 2026 10:00:00 +0200"
    msg.set_content("Anbei die Rechnung.")
    for name, mime, inhalt in anhaenge:
        haupt, unter = mime.split("/")
        msg.add_attachment(inhalt, maintype=haupt, subtype=unter, filename=name)
    return msg.as_bytes()


class FakeImap:
    """Minimaler IMAP-Server: ein Quellordner, UIDs ab 1, MOVE optional."""
    instanz = None

    def __init__(self, mails: dict[str, bytes], move: bool = True, uidplus: bool = True,
                 namespace: bytes | None = None, caps_erst_nach_login: bool = False):
        self.ordner = {"INBOX": dict(mails)}
        self._caps_voll = ("IMAP4REV1",) + (("MOVE",) if move else ()) + (("UIDPLUS",) if uidplus else ()) \
            + (("NAMESPACE",) if namespace else ())
        # Dovecot-Verhalten: vor dem Login nur Basis-Fähigkeiten
        self.capabilities = ("IMAP4REV1",) if caps_erst_nach_login else self._caps_voll
        self._namespace = namespace
        self.aktuell = None
        self.geloescht_markiert: set[str] = set()
        self.eingeloggt = False

    # imaplib.IMAP4_SSL(host, port, ssl_context=..., timeout=...)
    def __call__(self, *args, **kwargs):
        FakeImap.instanz = self
        return self

    def login(self, user, pw):
        if pw != "geheim":
            raise postfach.imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials")
        self.eingeloggt = True

    def select(self, name, readonly=False):
        name = name.strip('"')
        if name not in self.ordner:
            return "NO", [b"no such folder"]
        self.aktuell = name
        return "OK", [str(len(self.ordner[name])).encode()]

    def list(self, *a):
        if a == ('""', '""'):   # nur Trennzeichen erfragen
            return "OK", [b'(\\Noselect) "/" ""']
        return "OK", [f'(\\HasNoChildren) "." "{n}"'.encode() for n in self.ordner] + [b'(\\Noselect) "." "Shared"']

    def capability(self):
        return "OK", [" ".join(self._caps_voll).encode()]

    def namespace(self):
        if self._namespace is None:
            return "BAD", [b"Unknown command"]
        return "OK", [self._namespace]

    def create(self, name):
        self.ordner.setdefault(name.strip('"'), {})
        return "OK", [b""]

    def subscribe(self, name):
        return "OK", [b""]

    def logout(self):
        pass

    def uid(self, befehl, *args):
        befehl = befehl.upper()
        box = self.ordner[self.aktuell]
        if befehl == "SEARCH":
            return "OK", [" ".join(sorted(box, key=int)).encode()]
        uid = args[0].decode() if isinstance(args[0], bytes) else args[0]
        if befehl == "FETCH" and "RFC822.SIZE" in args[1]:
            return "OK", [f"1 (UID {uid} RFC822.SIZE {len(box[uid])})".encode()]
        if befehl == "FETCH":
            return "OK", [(f"1 (UID {uid} BODY[] {{{len(box[uid])}}}".encode(), box[uid]), b")"]
        if befehl in ("MOVE", "COPY"):
            ziel = args[1].strip('"')
            self.ordner[ziel][uid] = box[uid]
            if befehl == "MOVE":
                del box[uid]
            return "OK", [b""]
        if befehl == "STORE":
            self.geloescht_markiert.add(uid)
            return "OK", [b""]
        if befehl == "EXPUNGE":
            del box[uid]
            self.geloescht_markiert.discard(uid)
            return "OK", [b""]
        raise AssertionError(f"unerwarteter Befehl {befehl}")


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(postfach, "UPLOADS", tmp_path / "uploads")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(Unternehmen(
        firmenname="Test", strasse="S", hausnummer="1", plz="12345", ort="O",
        postfach_aktiv=True, imap_host="imap.example", imap_user="u", imap_passwort="geheim",
    ))
    session.commit()
    yield session
    session.close()


def _server(monkeypatch, mails, **kw) -> FakeImap:
    fake = FakeImap(mails, **kw)
    monkeypatch.setattr(postfach.imaplib, "IMAP4_SSL", fake)
    return fake


def test_abruf_legt_anhaenge_in_liste_und_verschiebt_mail(db, monkeypatch):
    fake = _server(monkeypatch, {
        "1": _mail("Rechnung 1", [("re1.pdf", "application/pdf", PDF)]),
        "2": _mail("E-Rechnung", [("re2.xml", "application/xml", XML)]),
        "3": _mail("Newsletter ohne Anhang", []),
    })

    erg = postfach.postfach_abrufen(db)

    assert (erg.mails_geprueft, erg.neu, erg.mails_verschoben, erg.dubletten) == (3, 2, 2, 0)
    eintraege = db.query(PostfachEingang).order_by(PostfachEingang.id).all()
    assert [e.original_name for e in eintraege] == ["re1.pdf", "re2.xml"]
    assert eintraege[0].betreff == "Rechnung 1" and "lieferant.example" in eintraege[0].absender
    assert all(e.status == "offen" for e in eintraege)
    assert (postfach.UPLOADS / eintraege[0].datei_pfad).read_bytes() == PDF
    # Mail ohne Anhang bleibt im Posteingang, die beiden anderen sind verschoben
    assert list(fake.ordner["INBOX"]) == ["3"]
    assert set(fake.ordner["RechnungsFee/verarbeitet"]) == {"1", "2"}


def test_zweiter_abruf_erkennt_dubletten(db, monkeypatch):
    _server(monkeypatch, {"1": _mail("Rechnung", [("re.pdf", "application/pdf", PDF)])})
    postfach.postfach_abrufen(db)
    # dieselbe Rechnung kommt noch einmal (z. B. Zahlungserinnerung mit gleichem Anhang)
    _server(monkeypatch, {"7": _mail("Erinnerung", [("re.pdf", "application/pdf", PDF)])})
    erg = postfach.postfach_abrufen(db)
    assert (erg.neu, erg.dubletten, erg.mails_verschoben) == (0, 1, 1)
    assert db.query(PostfachEingang).count() == 1


def test_bereits_als_beleg_vorhanden_ist_dublette(db, monkeypatch):
    import hashlib
    db.add(Beleg(dateiname="belege/x.pdf", original_name="x.pdf", sha256=hashlib.sha256(PDF).hexdigest()))
    db.commit()
    _server(monkeypatch, {"1": _mail("Rechnung", [("re.pdf", "application/pdf", PDF)])})
    erg = postfach.postfach_abrufen(db)
    assert (erg.neu, erg.dubletten) == (0, 1)


def test_zielordner_unter_inbox_namensraum(db, monkeypatch):
    # Courier/Dovecot mit Präfix "INBOX." - eigene Ordner nur unterhalb von INBOX erlaubt
    fake = _server(monkeypatch, {"1": _mail("Rechnung", [("re.pdf", "application/pdf", PDF)])},
                   namespace=b'(("INBOX." ".")) NIL NIL')
    erg = postfach.postfach_abrufen(db)
    assert erg.mails_verschoben == 1
    assert "1" in fake.ordner["INBOX.RechnungsFee.verarbeitet"]
    assert "RechnungsFee/verarbeitet" not in fake.ordner


def test_faehigkeiten_erst_nach_login_wie_dovecot(db, monkeypatch):
    # Der reale Fall aus dem Test: NAMESPACE/MOVE werden erst nach der Anmeldung gemeldet
    fake = _server(monkeypatch, {"1": _mail("Rechnung", [("re.pdf", "application/pdf", PDF)])},
                   namespace=b'(("INBOX." ".")) NIL NIL', caps_erst_nach_login=True)
    erg = postfach.postfach_abrufen(db)
    assert erg.mails_verschoben == 1 and not erg.uebersprungen
    assert "1" in fake.ordner["INBOX.RechnungsFee.verarbeitet"]
    assert "1" not in fake.ordner["INBOX"]   # per MOVE verschoben, nicht nur kopiert


def test_namensraum_ohne_praefix(db, monkeypatch):
    fake = _server(monkeypatch, {"1": _mail("Rechnung", [("re.pdf", "application/pdf", PDF)])},
                   namespace=b'(("" "/")) NIL NIL')
    postfach.postfach_abrufen(db)
    assert "1" in fake.ordner["RechnungsFee/verarbeitet"]


def test_ohne_move_kopieren_und_nur_eigene_mail_entfernen(db, monkeypatch):
    fake = _server(monkeypatch, {"1": _mail("Rechnung", [("re.pdf", "application/pdf", PDF)])},
                   move=False, uidplus=True)
    postfach.postfach_abrufen(db)
    assert "1" not in fake.ordner["INBOX"]
    assert "1" in fake.ordner["RechnungsFee/verarbeitet"]


def test_ohne_move_und_uidplus_bleibt_mail_nur_markiert(db, monkeypatch):
    fake = _server(monkeypatch, {"1": _mail("Rechnung", [("re.pdf", "application/pdf", PDF)])},
                   move=False, uidplus=False)
    postfach.postfach_abrufen(db)
    assert "1" in fake.ordner["INBOX"] and "1" in fake.geloescht_markiert
    assert "1" in fake.ordner["RechnungsFee/verarbeitet"]


def test_andere_anhaenge_werden_ignoriert(db, monkeypatch):
    fake = _server(monkeypatch, {"1": _mail("Bild", [("foto.jpg", "image/jpeg", b"\xff\xd8\xff")])})
    erg = postfach.postfach_abrufen(db)
    assert (erg.neu, erg.mails_verschoben) == (0, 0)
    assert list(fake.ordner["INBOX"]) == ["1"]


def test_falsches_passwort_gibt_verstaendliche_meldung(db, monkeypatch):
    _server(monkeypatch, {})
    db.query(Unternehmen).first().imap_passwort = "falsch"
    db.commit()
    with pytest.raises(HTTPException) as exc:
        postfach.postfach_abrufen(db)
    assert "Anmeldung am Postfach fehlgeschlagen" in exc.value.detail


def test_falscher_ordner_nennt_vorhandene_ordner(db, monkeypatch):
    fake = _server(monkeypatch, {})
    fake.ordner["INBOX.TEST"] = {}
    db.query(Unternehmen).first().imap_ordner = "INBOX/TEST"
    db.commit()
    with pytest.raises(HTTPException) as exc:
        postfach.postfach_testen(db)
    assert "„INBOX/TEST\" nicht gefunden" in exc.value.detail
    assert "„INBOX.TEST“" in exc.value.detail and "„INBOX“" in exc.value.detail
    assert "Shared" not in exc.value.detail   # \Noselect nicht anbieten


def test_nicht_eingerichtet(db):
    db.query(Unternehmen).first().imap_host = None
    db.commit()
    with pytest.raises(HTTPException) as exc:
        postfach.postfach_abrufen(db)
    assert "nicht vollständig eingerichtet" in exc.value.detail


def test_uebernehmen_und_verwerfen_entfernen_datei_behalten_zeile(db, monkeypatch):
    _server(monkeypatch, {
        "1": _mail("A", [("a.pdf", "application/pdf", PDF)]),
        "2": _mail("B", [("b.pdf", "application/pdf", PDF + b"x")]),
    })
    postfach.postfach_abrufen(db)
    a, b = db.query(PostfachEingang).order_by(PostfachEingang.id).all()
    pfad_a = postfach.UPLOADS / a.datei_pfad

    postfach.postfach_uebernommen(a.id, postfach.UebernommenRequest(rechnung_id=None), db)
    postfach.postfach_verwerfen(b.id, db)

    assert not pfad_a.exists()
    assert [e.status for e in db.query(PostfachEingang).order_by(PostfachEingang.id)] == ["uebernommen", "verworfen"]
    assert postfach.postfach_eingang("offen", db) == []
