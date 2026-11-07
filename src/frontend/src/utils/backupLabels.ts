const BACKUP_TYPEN: Record<string, string> = {
  manual: 'Manuell',
  exit: 'Beim Beenden',
  pre_migration: 'Vor einem Update',
  pre_restore: 'Vor einer Wiederherstellung',
}

const UMFAENGE: Record<string, string> = {
  voll: 'Daten und Belege',
  nur_db: 'Nur Datenbank',
}

const ORTE: Record<string, string> = {
  lokal: 'Lokal',
  extern: 'Extern',
  download: 'Download',
}

export const backupTypLabel = (typ: string | null) => (typ ? BACKUP_TYPEN[typ] ?? typ : null)
export const backupUmfangLabel = (umfang: string | null) => (umfang ? UMFAENGE[umfang] ?? umfang : null)
export const backupOrtLabel = (ort: string) => ORTE[ort] ?? ort
