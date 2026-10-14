# Changelog

## 2026.10.4

- Mehrere Apple-IDs: Unter „Konten“ beliebig viele Konten anlegen, jedes mit eigener Anmeldung, eigenem Zielordner und eigener Auswahl Drive/Fotos. Gesichert wird nacheinander im gemeinsamen Intervall.
- Das bisherige Konto wird beim Update übernommen und behält sein Ziel, es wird nichts neu heruntergeladen.
- Ablauf-Warnungen und Fehlermeldungen nennen das betroffene Konto.

## 2026.10.3

- iCloud Drive: Paket-Dokumente (Pages, Numbers, Keynote, Pixelmator …) brechen nicht mehr mit „corrupted on transfer: sizes differ“ ab. iCloud meldet für sie die entpackte Größe, liefert aber ein ZIP.
- Fehlermeldungen in Übersicht und Protokoll nennen jetzt die betroffene Datei.

## 2026.10.2

- Eigenes Icon und Banner im Add-on-Store.

## 2026.10.1

- Erste Version: iCloud Drive und iCloud-Fotos per rclone 1.75.2 auf eine Netzwerkfreigabe kopieren.
- Oberfläche mit Übersicht, Fortschritt, Anmeldung samt 2FA/SMS, Einstellungen und Protokoll.
- Benachrichtigungen in Home Assistant bei Fehlern und vor Ablauf der 30-Tage-Anmeldung.
