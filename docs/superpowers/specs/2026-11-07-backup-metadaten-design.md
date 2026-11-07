# Backup-Metadaten – Design

Stand: 2026-11-07. Branch: `feature/backup-metadaten`.

## Ziel

RechnungsFee erkennt das neueste passende Backup unabhängig von Dateiname und
Speicherort und zeigt vor dem Wiederherstellen dessen Metadaten an. Backups ohne
Metadaten bleiben wiederherstellbar.

## Abhängigkeit

Das Design setzt auf der noch nicht committeten SQL-Treiber-Arbeit auf
(`database/portable_backup.py`, Testdaten-Isolation in `tests/conftest.py`). Nur
`utils/backup_metadata.py` ist davon unabhängig. Alle Änderungen an `main.py`,
`api/backup.py` und `portable_backup.py` folgen, sobald diese Arbeit committet ist
und der Branch darauf aufgesetzt wurde.

Backend-Tests, die `database.connection` importieren, dürfen vorher nicht laufen:
Ohne die Isolation schreiben sie in das echte Profil.

## Bausteine

| Baustein | Aufgabe |
|---|---|
| Root-`manifest.json` | Liegt in jedem ZIP (SQLite und portabel), letzter Eintrag des Archivs. `database/manifest.json` bleibt unverändert. |
| `backups/backup-verlauf.jsonl` | Lokaler Verlauf. Jedes Backup hängt eine Zeile an, auch externe und Downloads. |
| `profil_id.txt` im Profilordner | Stabile Profilkennung. Wird beim ZIP-Restore aus dem Manifest übernommen. |
| `utils/backup_metadata.py` | Manifest, Verlauf, Profilkennung, Sortierung, Archivprüfung. Kein Zugriff auf Datenbank oder API. |
| Gemeinsame ZIP-Erstellung | Ersetzt die doppelte Logik in `main.py` und `api/backup.py`. |

Der Verlauf ist bewusst eine Datei und keine Tabelle: Ein Restore überschreibt die
Datenbank, `backups/` bleibt dabei unberührt und ist bereits pro Profil getrennt.

Die Profilkennung liegt bewusst nicht in der Datenbank: Das spart eine Migration für
drei Dialekte und ist lesbar, bevor die Datenbank geöffnet wird. Geht die Datei
verloren, entsteht eine neue Kennung; die Folge ist höchstens der Hinweis „anderes
Profil“.

## Manifest

```json
{
  "manifest_version": 1,
  "application": "RechnungsFee",
  "backup_id": "b7f3c8a1-6e42-4f90-a8cb-d7e3c112ba65",
  "app_version": "0.7.8",
  "schema_version": 181,
  "profile_id": "5d0c3f1e-…",
  "profile_name": "Standard",
  "backup_type": "manual",
  "created_at": "2026-11-07T19:45:00Z",
  "completed_at": "2026-11-07T19:45:06Z",
  "contents": {"format": "sqlite", "database": true, "uploads": true, "file_count": 148},
  "integrity": {"algorithm": "sha256", "files": {"rechnungsfee.db": "<hex>"}}
}
```

- `backup_type`: `manual`, `exit`, `pre_migration`, `pre_restore`.
- `contents.format`: `sqlite` oder `portable`.
- `integrity.files`: Pfad und SHA-256 der Datenbankinhalte. Uploads sichert die
  CRC-Prüfung des ZIP.
- Kein `status`: Dateien entstehen atomar (temporäre Datei, dann umbenennen). Was
  existiert, ist vollständig.
- Kein `encrypted`: wird aus der Dateiendung `.zip.enc` abgeleitet.
- Ältere Programmversionen ignorieren die zusätzliche Datei im ZIP.

SHA-256 erkennt Beschädigung, aber keine gezielte Manipulation, denn die Prüfsumme
lässt sich neu berechnen. Bei `.zip.enc` schützt AES-GCM zusätzlich die Authentizität.

## Verlauf

Eine JSON-Zeile pro Backup mit `backup_id`, `dateiname`, `ort` (`lokal`, `extern`,
`download`), `ziel`, `backup_type`, `umfang` (`voll`, `nur_db`), `created_at`,
`completed_at`, `app_version`, `schema_version`, `profile_id`, `groesse` und
`integrity`.

- Kaputte Zeilen werden übersprungen.
- Der Verlauf behält die letzten 500 Einträge.
- Externe Einträge entstehen nur pro erfolgreich geschriebenem Ziel.
- Externe Einträge und Downloads sind reine Anzeige: Der Verlauf belegt nicht, dass
  die Datei noch existiert. Eingespielt wird per Upload.
- Lokale `.db`-Snapshots bekommen keine Sidecar-Dateien; ihre Metadaten stehen im
  Verlauf. Ein kopierter `.db`-Snapshot lässt sich ohnehin nicht hochladen.

## Sortierung

`completed_at` absteigend, bei Gleichstand `backup_id` absteigend. Einträge ohne
gültigen Zeitstempel stehen am Ende. Sicherheitsbackups (`pre_*`) werden nie als
„neueste“ markiert.

## Prüfregeln beim Restore

`POST /api/backup/inspect` schreibt nichts und funktioniert auch vor der
Ersteinrichtung. Dieselbe Prüfung läuft in `/wiederherstellen` und
`/wiederherstellen-lokal`.

| Ergebnis | Code | Fall |
|---|---|---|
| Blockiert | – | Kein gültiges ZIP oder falsches Passwort (vor der Archivprüfung) |
| Blockiert | `keine_datenbank` | Keine Datenbank im Backup |
| Blockiert | `hash_abweichung` | Hash der Datenbankinhalte stimmt nicht oder Datei fehlt |
| Blockiert | `schema_zu_neu` | Schema neuer als die App |
| Blockiert | `manifest_zu_neu` | `manifest_version` neuer als die App |
| Blockiert | `unsicherer_pfad` | Upload-Pfad führt aus `uploads/` heraus |
| Warnung | `manifest_fehlt` | Kein Manifest |
| Warnung | `manifest_defekt` | Manifest unlesbar oder ungültig |
| Warnung | `schema_unbekannt` | Schema nicht ermittelbar |
| Warnung | `anderes_profil` | Profilkennung weicht ab |
| Warnung | `neueres_backup_bekannt` | Im Verlauf gibt es ein neueres Backup |
| Warnung | `nur_datenbank` | Keine Uploads enthalten, Belege bleiben unverändert |
| Info | `schema_aelter` | Wird beim Start migriert |

Ein defektes Manifest blockiert bewusst nicht: Die Datenbank kann intakt sein, und
ein Totalverlust ist der schlechteste Moment für eine Sperre. Das Backend setzt nur
Blockaden durch, die Bestätigung der Warnungen liegt in der Oberfläche.

Ohne Manifest wird das Schema aus dem SQLite-Header (`user_version`) oder aus
`database/manifest.json` gelesen.

Der Zip-Slip wird zusätzlich im Restore selbst behoben (`_prüfe_wiederherstellung`).

## API

| Endpunkt | Änderung |
|---|---|
| `GET /api/backup/liste` | Verbindet Backup-Ordner und Verlauf. Neu: `backup_type`, `umfang`, `ort`, `app_version`, `schema_version`, `hat_metadaten`, `wiederherstellbar`, `ist_neueste_vollsicherung`. |
| `POST /api/backup/inspect` | Neu. Datei und optional Passwort, liefert Metadaten, Blockaden, Warnungen, Infos. |
| `POST /api/backup/wiederherstellen` | Antwortet bei Blockaden mit 400. |
| `POST /api/backup/wiederherstellen-lokal` | Prüft zusätzlich Schema und Hash aus dem Verlauf. |

`GET /api/backup/latest` entfällt vorerst, weil es keinen Abnehmer gibt. Die
Oberfläche bekommt die Markierung aus `/liste`.

## Oberfläche

- `BackupPage`: Typ, Umfang, Ort, Version, Markierung „Neueste Vollsicherung“. Ohne
  Verlaufseintrag: „Metadaten nicht vorhanden“.
- `BackupZipWiederherstellen`: Datei wählen und prüfen, Metadaten und Warnungen
  sehen, bestätigen, einspielen. Die Datei geht dabei zweimal an das lokale Backend.

## App-Version

Der Sidecar wird in `build.yml` gebaut, bevor die Version in `tauri.conf.json`
gesetzt wird. Deshalb schreibt ein neuer Schritt vor dem Sidecar-Bau
`src/backend/_app_version.py` aus `GITHUB_REF_NAME`, derselben Quelle wie im
Frontend. Lokal gilt `dev`.

## Tests

- Manifest: beschädigt, fehlend, verändert, neuere Version
- Verlauf: kaputte Zeilen, Kürzung, Sortierung bei Gleichstand
- Restore: Blockaden, Warnungen, alte Backups ohne Manifest, Zip-Slip
- Erstellung: Round-Trip für SQLite und portabel, Übernahme der Profilkennung

## Akzeptanzkriterien

- Jedes neue ZIP-Backup enthält eine gültige `manifest.json`.
- Jedes Backup steht im Verlauf, lokale `.db`-Snapshots eingeschlossen.
- Bei der Auswahl werden Datum, Profil und Version angezeigt.
- Die neueste Vollsicherung wird korrekt markiert, Sicherheitsbackups nie.
- ZIP-Backups lassen sich auch nach dem Kopieren auf andere Datenträger einordnen.
- Verschlüsselte Backups lassen sich nach Passworteingabe prüfen.
- Ältere Backups bleiben lesbar und wiederherstellbar.
- Tests decken beschädigte, fehlende und veränderte Metadaten ab.

## Bekannte Folgearbeit außerhalb dieser Änderung

`_pruefe_manifest` in `portable_backup.py` verlangt exakt dieselbe Schemaversion.
Portable Backups wären damit nach jeder Schemaänderung nicht mehr einspielbar. Das
gehört zur SQL-Treiber-Arbeit und sollte vor deren Release gelöst sein.
