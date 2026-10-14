![iCloud Backup für Home Assistant](https://raw.githubusercontent.com/dhaucke/ha-icloud-backup/main/assets/icloud-backup-banner.png)

# iCloud Backup für Home Assistant

Home-Assistant-Add-on, das **iCloud Drive** und **iCloud-Fotos** einer oder mehrerer Apple-IDs regelmäßig auf eine Netzwerkfreigabe kopiert.
Die Arbeit macht [rclone](https://rclone.org/iclouddrive/), das Add-on liefert Oberfläche, Zeitplan, 2FA-Anmeldung
und Benachrichtigungen in Home Assistant.

## Installation

1. Home Assistant → **Einstellungen → Add-ons → Add-on-Store → ⋮ → Repositorys**
   → `https://github.com/dhaucke/ha-icloud-backup` hinzufügen.
2. **iCloud Backup** installieren und starten, „In Seitenleiste anzeigen“ einschalten.

## Einrichtung

1. Netzwerkfreigabe einbinden: **Einstellungen → System → Speicher → Netzwerkspeicher hinzufügen**,
   Verwendung **Freigabe**. Danach das Add-on neu starten.
2. Auf dem iPhone **Einstellungen → Apple-Account → iCloud → „Auf iCloud-Daten im Web zugreifen“** einschalten.
3. Im Add-on unter **Konten → Konto hinzufügen** Name, Zielordner (z. B. `/share/nas-backup/Denis`),
   Apple-ID und normales Passwort eingeben (keine app-spezifischen Passwörter), dann den 2FA-Code.
   Ist Erweiterter Datenschutz (ADP) aktiv, den Zugriff danach auf einem Apple-Gerät erlauben.
4. Für jede weitere Apple-ID Schritt 2 und 3 wiederholen, jeweils mit eigenem Zielordner.
   Der 2FA-Code kommt auf den Geräten der jeweiligen Apple-ID an.

## Verhalten

- Ziel je Konto: `<Ziel>/iCloud Drive` und `<Ziel>/iCloud Fotos/<Mediathek>/All Photos`.
- Alle Konten werden nacheinander im gemeinsamen Intervall gesichert (**Einstellungen**).
- Es wird nur **kopiert** (`rclone copy`): In iCloud gelöschte Dateien bleiben in der Sicherung,
  geänderte Dateien werden überschrieben.
- Gesichert wird nur auf eingebundene Freigaben/Laufwerke, nie auf die Home-Assistant-Festplatte.
- Apple verlangt die Anmeldung je Konto alle **30 Tage** neu. Fünf Tage vorher und bei Fehlern erscheint eine
  Benachrichtigung in Home Assistant.
- `rclone.conf` (verschleiertes Passwort, Vertrauenstoken) ist von Home-Assistant-Backups ausgenommen;
  nach einer Wiederherstellung einmal neu anmelden.

## Entwicklung

```
python3 -m unittest discover -s tests -v
```

Die Tests nutzen ein nachgebautes rclone (`tests/fake_rclone.py`) und sprechen nie mit Apple.
