# Tapo C200 über Home Assistant in Frigate nutzen

Die entfernte Kamera bleibt in ihrem eigenen WLAN. Home Assistant meldet sich über Topos Cloud-Verfahren an und stellt das Video als Kamera-Entität bereit. Dashboard, JPEG-Aufnahmen und der Frigate-Ausgang nutzen den HLS-Stream dieser Entität. Frigates mitgeliefertes go2rtc liest ihn und stellt ein lokales RTSP-Signal bereit. Die Desktop-App muss dafür an keinem der beiden Standorte laufen. Die Integration ist für Livevideo ausgelegt. Ton und Gegensprechen sind nicht Bestandteil dieser Version.

## 1. In Home Assistant installieren

Öffne **HACS → ⋮ → Benutzerdefinierte Repositories**, trage die GitHub-URL dieses Repositorys ein und wähle den Typ **Integration**. Lade anschließend **Tapo Cloud C200** in HACS herunter.

Starte Home Assistant neu. Öffne **Einstellungen → Geräte & Dienste → Integration hinzufügen → Tapo Cloud C200**. Melde dich mit der E-Mail-Adresse und dem Passwort deines Tapo-Kontos an. Falls Tapo einen zweiten Faktor anfordert, wähle die Methode und gib den zugeschickten Code ein. Wähle anschließend die C200 aus der Liste der Kameras im Konto. Bei mehreren Kameras kannst du die Integration für jede Kamera erneut hinzufügen.

Öffne die neue Kamera-Entität und prüfe das Livebild. Kopiere ihre tatsächliche **Entitäts-ID**, beispielsweise `camera.garten`. Für das Dashboard eignet sich die Karte „Bildentität“ mit der Kameraansicht „Live“.

Der Sitzungstoken liegt in Home Assistants Konfiguration und ist wie ein Passwort zu schützen. Das Tapo-Passwort und der MFA-Code werden nicht im Konfigurationseintrag gespeichert. Bei abgelaufener Sitzung kannst du dich unter **Geräte & Dienste → Tapo Cloud C200 → Neu konfigurieren** erneut anmelden.

## 2. Den Eingang für Frigate einschalten

1. Öffne beim Integrationseintrag der Kamera **Konfigurieren** (Zahnrad).
2. Aktiviere **Frigate-Zugriff** und setze ein separates, zufälliges Lesepasswort mit 20–128 Zeichen. Sonderzeichen, Leerzeichen und Umlaute sind erlaubt. Der Benutzer heißt `frigate`. Zum Abschalten entfernst du den Haken. Um das Passwort zu wechseln, gib ein neues ein.
3. Übernimm die Einträge aus **Frigate-Beispiel.yml** in die gleichnamigen Abschnitte deiner Frigate-Konfiguration. Ersetze `HA-IP`, `PASSWORT` und `camera.tapo_c200`. Gib das Passwort im HA-Dialog unverändert ein. In der Stream-URL muss nur der Passwortteil URL-kodiert sein (UTF-8), etwa `@` als `%40`, `#` als `%23`, `%` als `%25` und Leerzeichen als `%20`. Behalte `#video=copy` am Ende der URL bei. Bei HTTPS verwende den passenden Hostnamen und Port mit einem von Frigate akzeptierten Zertifikat. HTTP mit Basic-Authentifizierung ist für ein vertrauenswürdiges LAN gedacht.
4. Speichere die Frigate-Konfiguration und starte Frigate neu. Das Livebild sollte unter **tapo_cloud** erscheinen. go2rtc verwendet Video-Copy ohne Neukodierung. Bei getrennter Installation von go2rtc und Frigate passe die RTSP-Adresse an. Für Aufzeichnungen in Frigate ergänze die Rolle `record` und aktiviere die Aufnahme gemäß deinen Aufbewahrungseinstellungen.

Home Assistant stellt einen stabilen Einstiegspunkt pro Kamera bereit. Bei erfolgreicher Anmeldung leitet er auf die zur laufenden Sitzung gehörende HLS-Wiedergabeliste weiter. Verwende dauerhaft den Einstiegspunkt aus dem Beispiel. Die HLS-Segmentierung kann einige Sekunden Verzögerung verursachen.

## 3. Jede Minute ein JPEG speichern

Öffne **Einstellungen → Automationen & Szenen → Blaupausen**. Erstelle aus **Tapo C200 · Zeitraffer mit einem Bild pro Minute** eine Automation, wähle die Kamera, lege den Zielordner fest und speichere. Ein erster Test ist über **Aktionen ausführen** im Automationsmenü möglich.

Die Vorgabe ist `/media/c200-zeitraffer`. Dieses Verzeichnis muss für HA beschreibbar sein und in einem erlaubten Medienpfad liegen. Binde bei Home Assistant Container **/media als persistentes Volume** ein, wenn du den Standardpfad verwendest. Alternativ gib einen bereits dauerhaft eingebundenen Medienordner an. Bei freigegebenem übergeordnetem Pfad legt HA die Unterordner für Kamera und Tag an.

Beispiel für einen Ausgabeort:

```text
/media/c200-zeitraffer/garten/2026-11-21/2026-11-21_12-30-00.jpg
```

Zeitstempel und Tagesordner verwenden UTC. Das erhält bei der Zeitumstellung die Reihenfolge. Pro vollem Tag entstehen bis zu 1.440 Bilder. Die Vorlage löscht keine Aufnahmen. Während eines Cloud-Ausfalls können Bilder fehlen.

**Film eines Tages erstellen:** Öffne auf einem Linux-Rechner oder Mac mit FFmpeg ein Terminal im gewünschten Tagesordner. Dieser Befehl erzeugt aus den zeitlich sortierten JPEGs eine MP4 mit 25 Bildern pro Sekunde. Bei 1.440 Bildern dauert sie 57,6 Sekunden.

```bash
ffmpeg -n -framerate 25 -pattern_type glob -i '*.jpg' -c:v libx264 -pix_fmt yuv420p -movflags +faststart zeitraffer.mp4
```

**Einen normalen Videoclip in HA aufnehmen:** Unter **Entwicklerwerkzeuge → Aktionen** ist die Aktion `camera.record` verfügbar. Beispiel für den YAML-Modus:

```yaml
action: camera.record
target:
  entity_id: camera.garten
data:
  duration: 30
  lookback: 0
  filename: "/media/c200-{{ utcnow().strftime('%Y%m%d-%H%M%S') }}.mp4"
```

## Bei fehlendem Bild

- **HA zeigt kein Bild:** Prüfe zuerst, ob die entfernte C200 mit demselben Konto in der Tapo-App erreichbar ist. Nach dem ersten Start braucht das Relay Zeit für die Verbindung und das erste Schlüsselbild. Wenn der erste Snapshot abbricht, öffne das Livebild und versuche es erneut. Die Einstellung „Stream vorladen“ der Kamera kann Wartezeiten reduzieren, hält jedoch eine Verbindung zum Kamerastandort aktiv.
- **401 bei Frigate:** Prüfe Benutzer `frigate` und das im HA-Zahnrad gesetzte Lesepasswort. Schütze die Frigate-Konfiguration und ihre Backups, da sie die Zugangsdaten enthalten.
- **404 beim Einstiegspunkt:** Übernimm die genaue Kamera-Entitäts-ID aus HA und prüfe, ob der Frigate-Zugriff für diese Kamera aktiviert ist.
- **Die Verbindung bricht wiederholt ab:** Prüfe **Einstellungen → System → Protokolle** für die Integration und den Stream. Topo nutzt undokumentierte Cloud-Schnittstellen. Kamera-Firmware, Sitzungslaufzeit und Änderungen bei TP-Link können den Betrieb beeinflussen. Das in Topo als verschlüsselt markierte zusätzliche Medienformat wird hier nicht dekodiert.
- **HA meldet einen unzulässigen Dateipfad:** Wähle einen Pfad unter einem in HA eingetragenen Medienordner und prüfe die Schreibrechte des HA-Containers.

## Version und Prüfumfang

Die Integration verwendet Teile des [Topo-Cloudclients](https://github.com/filirnd/topo/tree/e191c68e5b8ae9d006ebd69257644b113ad8c116). Die MIT-Lizenz liegt im Komponentenordner. Hinweise zum Datenpfad findest du auch in der [HA-Dokumentation für Kamera-Entitäten](https://developers.home-assistant.io/docs/core/entity/camera/).

Lokal geprüft mit Home Assistant Core 2026.11.2, einem simulierten Tapo-Konto und einem lokalen TLS-Relay mit synthetischem H.264-Video. Der Test umfasst Einrichtungsdialog mit MFA und Kameraauswahl, Wiedergabelisten mit Mediensegmenten, zwei unterschiedliche Standbilder, eine dekodierbare MP4, den Minutentrigger und die Zugangsprüfung für Frigate. Zusätzlich wurde mit go2rtc 1.9.15 aus einem kalten Start ein Bild über den RTSP-Ausgang per FFmpeg dekodiert. Der reale Tapo-Login, der Dauerbetrieb mit deiner C200 und der Einsatz in deiner Frigate-Installation stehen noch aus.
