"""
Event-Pipeline: verbindet Speichervorgänge der App mit den Plugin-Hooks
(docs/plan-plugin-system.md, Phase 5).

Zentral über SQLAlchemy-Session-Events auf `SessionLocal` - die bestehenden Endpunkte bleiben
unverändert.

Ablauf je Transaktion:
1. `before_flush`  - zu löschende Kunden/Rechnungen/Artikel als DTO sichern (danach sind sie weg).
2. `after_flush`   - neue/geänderte Objekte merken (IDs sind jetzt vergeben). Geänderte
                     Rechnungspositionen zählen als Änderung ihrer Rechnung. Endpunkte flushen oft
                     mehrfach (erst Rechnung für die ID, dann Positionen) - deshalb nur sammeln.
3. `before_commit` - Pre-Hooks mit dem VOLLSTÄNDIGEN Stand ausführen. Geänderte Felder (nur die
                     erlaubten, siehe plugins/dto.py) zurückschreiben. `PluginAbbruch` verhindert den
                     Commit; der Endpunkt antwortet mit 422 (Exception-Handler in main.py).
4. `after_commit`  - Post-Hooks im Hintergrund anstoßen.
5. `after_rollback`/`after_soft_rollback` - Gesammeltes verwerfen.

Bewusste Lücken: Massen-Updates per `query.update()/delete()` und rohes SQL (`text(...)`) laufen
an den ORM-Events vorbei. Für Kunden/Rechnungen/Artikel betrifft das nur Migrationen und
Datenkorrekturen - genau dort sollen Plugins auch nicht eingreifen.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import time

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from database.models import Rechnung, Rechnungsposition

from . import dto, laufzeit
from .host_api import LeseZugriff

log = logging.getLogger("rechnungsfee.plugins")

_SCHLUESSEL = "_plugin_ereignisse"     # session.info: {(entity, id): {"action", "vorher"}}
_POST = "_plugin_post"                  # session.info: [(entity, action, dto)]
_IN_ARBEIT = "_plugin_in_arbeit"

_unterdrueckt: contextvars.ContextVar[bool] = contextvars.ContextVar("plugins_unterdrueckt", default=False)


@contextlib.contextmanager
def ohne_plugins():
    """Plugins für einen Block abschalten - App-Start, Datenübernahme/Massenimport. Dort sollen
    weder Prüf-Plugins einzelne Zeilen abweisen noch Post-Hooks pro Zeile feuern."""
    token = _unterdrueckt.set(True)
    try:
        yield
    finally:
        _unterdrueckt.reset(token)


def plugins_aus(fn):
    """Dekorator-Variante von ohne_plugins() für Endpunkte (sync und async)."""
    import functools
    import inspect as _inspect

    if _inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def asynchron(*args, **kwargs):
            with ohne_plugins():
                return await fn(*args, **kwargs)
        return asynchron

    @functools.wraps(fn)
    def synchron(*args, **kwargs):
        with ohne_plugins():
            return fn(*args, **kwargs)
    return synchron


def _aktiv(session: Session) -> bool:
    return not _unterdrueckt.get() and not session.info.get(_IN_ARBEIT)


def _merke(session: Session, entity: str, obj_id, action: str, vorher: dict | None = None) -> None:
    ereignisse = session.info.setdefault(_SCHLUESSEL, {})
    schluessel = (entity, obj_id)
    bisher = ereignisse.get(schluessel)
    if bisher is None:
        ereignisse[schluessel] = {"action": action, "vorher": vorher}
    elif action == "delete":
        if bisher["action"] == "create":
            del ereignisse[schluessel]          # in derselben Transaktion angelegt und gelöscht
        else:
            ereignisse[schluessel] = {"action": "delete", "vorher": vorher}
    # create bleibt create, update bleibt update


def _before_flush(session: Session, _ctx, _instanzen) -> None:
    if not _aktiv(session) or not session.deleted or not laufzeit.irgendein_plugin():
        return
    for obj in session.deleted:
        d = dto.NACH_MODELL.get(type(obj))
        if d is None or obj.id is None:
            continue
        if _relevant(d.name, "delete"):
            _merke(session, d.name, obj.id, "delete", dto.als_dto(obj))


def _relevant(entity: str, action: str) -> bool:
    return laufzeit.hat_hooks(entity, action, "before") or laufzeit.hat_hooks(entity, action, "after")


def _after_flush(session: Session, _ctx) -> None:
    if not _aktiv(session):
        return
    if not (session.new or session.dirty or session.deleted) or not laufzeit.irgendein_plugin():
        return   # schneller Ausstieg: ohne laufende Plugins kostet die Pipeline praktisch nichts
    for obj in session.new:
        d = dto.NACH_MODELL.get(type(obj))
        if d is not None:
            _merke(session, d.name, obj.id, "create")
        elif isinstance(obj, Rechnungsposition) and obj.rechnung_id:
            _merke(session, "invoice", obj.rechnung_id, "update")
    for obj in session.dirty:
        d = dto.NACH_MODELL.get(type(obj))
        if d is not None:
            if session.is_modified(obj, include_collections=False):
                _merke(session, d.name, obj.id, "update")
        elif isinstance(obj, Rechnungsposition) and obj.rechnung_id \
                and session.is_modified(obj, include_collections=False):
            _merke(session, "invoice", obj.rechnung_id, "update")
    for obj in session.deleted:
        if isinstance(obj, Rechnungsposition) and obj.rechnung_id:
            _merke(session, "invoice", obj.rechnung_id, "update")


def _before_commit(session: Session) -> None:
    if not _aktiv(session):
        return
    if session.new or session.dirty or session.deleted:
        session.flush()   # noch Ausstehendes erfassen (after_flush sammelt)
    ereignisse = session.info.pop(_SCHLUESSEL, None)
    if not ereignisse:
        return

    session.info[_IN_ARBEIT] = True
    try:
        post = session.info.setdefault(_POST, [])
        host = LeseZugriff(session=session)
        start = time.perf_counter()
        for (entity, obj_id), e in list(ereignisse.items()):
            action = e["action"]
            # Gemeinsames Budget für die ganze Transaktion (NFA-3)
            rest = laufzeit.PRE_HOOK_BUDGET_MS - (time.perf_counter() - start) * 1000

            if action == "delete":
                vorher = e["vorher"]
                if vorher is None:
                    continue
                if laufzeit.hat_hooks(entity, "delete", "before"):
                    laufzeit.vor_aenderung(entity, "delete", vorher, host=host, budget_ms=max(rest, 0.001))
                post.append((entity, "delete", vorher))
                continue

            if not _relevant(entity, action):
                continue
            obj = session.get(dto.ENTITAETEN[entity].modell, obj_id)
            if obj is None or inspect(obj).deleted:
                continue
            vorher = dto.als_dto(obj)
            nachher = vorher
            if laufzeit.hat_hooks(entity, action, "before"):
                nachher = laufzeit.vor_aenderung(entity, action, vorher, host=host, budget_ms=max(rest, 0.001))
            if nachher != vorher:
                _uebernehme(entity, action, obj, vorher, nachher)
            post.append((entity, action, dto.als_dto(obj) if nachher != vorher else vorher))
        session.flush()   # vom Plugin übernommene Änderungen schreiben, solange _IN_ARBEIT gilt
    except BaseException:
        session.info.pop(_POST, None)
        raise
    finally:
        session.info.pop(_IN_ARBEIT, None)


def _uebernehme(entity: str, action: str, obj, vorher: dict, nachher: dict) -> None:
    erg = dto.uebernehme(obj, vorher, nachher)
    hook = f"{entity}:{action}:before"
    if erg.ignoriert or erg.ungueltig:
        teile = []
        if erg.ignoriert:
            teile.append("ignoriert: " + ", ".join(erg.ignoriert))
        if erg.ungueltig:
            teile.append("ungültig: " + "; ".join(erg.ungueltig))
        # welchem Plugin das gehört, weiß hier niemand mehr genau (Kette) - deshalb als Hinweis
        # unter dem Pseudo-Plugin der Pipeline protokollieren
        laufzeit._protokolliere("rechnungsfee.pipeline", hook, "teilweise",
                                f"{entity} {obj.id}: " + " | ".join(teile), entitaet=entity)
        log.info("Plugin-Änderung an %s %s teilweise übernommen: %s", entity, obj.id, teile)


def _after_commit(session: Session) -> None:
    post = session.info.pop(_POST, None)
    if not post or _unterdrueckt.get():
        return
    from database.connection import SessionLocal

    for entity, action, payload in post:
        laufzeit.nach_aenderung(entity, action, payload,
                                host_factory=lambda: LeseZugriff(session_factory=SessionLocal))


def _after_rollback(session: Session, *_args) -> None:
    session.info.pop(_SCHLUESSEL, None)
    session.info.pop(_POST, None)
    session.info.pop(_IN_ARBEIT, None)


_MARKIERUNG = "_rechnungsfee_plugin_pipeline"


def registrieren(ziel) -> None:
    """Hängt die Pipeline an eine Session-Factory (sessionmaker) oder Session-Klasse.
    Mehrfachaufruf für dasselbe Ziel ist harmlos.

    Markierung direkt am Ziel-Objekt statt id(ziel) in einem Set oder event.contains(): Python
    vergibt die id() einer freigegebenen Factory neu, und SQLAlchemys contains()-Buchhaltung hängt
    ebenfalls an id(). Eine neue Factory hielt sich dadurch zufällig für bereits registriert und
    lief ohne Plugins (aufgefallen als wackelige Tests)."""
    if getattr(ziel, _MARKIERUNG, False):
        return
    setattr(ziel, _MARKIERUNG, True)
    event.listen(ziel, "before_flush", _before_flush)
    event.listen(ziel, "after_flush", _after_flush)
    event.listen(ziel, "before_commit", _before_commit)
    event.listen(ziel, "after_commit", _after_commit)
    event.listen(ziel, "after_rollback", _after_rollback)
    event.listen(ziel, "after_soft_rollback", _after_rollback)


__all__ = ["registrieren", "ohne_plugins", "plugins_aus", "Rechnung"]
