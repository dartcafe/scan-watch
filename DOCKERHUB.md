# scan-watch

Scans automatically as soon as paper is placed in the feeder of a network document
scanner and stores each stack (or each sheet) as one PDF in a folder and/or uploads
it to [Paperless-ngx](https://docs.paperless-ngx.com/). Built for the Canon DR-S150,
but any scanner speaking eSCL (AirScan / Mopria) with ADF paper detection should work.

No vendor software and no button presses on the scanner: put paper in, walk away.
A web interface shows the scanner state live, previews of the last documents and
all settings, also on the phone next to the scanner.

<img src="https://raw.githubusercontent.com/dartcafe/scan-watch/main/images/web_ui.png" alt="Web interface: status, recent documents and profiles" width="600">

- Color or grayscale, 100–300 dpi, simplex or duplex, A4, A5, Letter or Legal
- Blank backs of duplex scans are dropped; a page with only a page number is kept
- One PDF per stack or one per sheet; profiles switch settings with one click
- Paperless upload with tags, self-signed certificates can be pinned
- Never loses a scan: if Paperless is down, the PDF goes to the folder
- English and German, following the browser

Source, full documentation and changelog: **https://github.com/dartcafe/scan-watch**

## Tags

| Tag              | Meaning                                  |
| ---------------- | ---------------------------------------- |
| `1.0.1`, …       | exactly this release                     |
| `1.0`            | latest release of 1.0 (bug fixes only)   |
| `1`              | latest release of 1.x                    |
| `latest`         | latest release                           |

Images for `linux/amd64` and `linux/arm64`.

## Quick start (Docker Compose)

Create the two folders first, owned by the user the container runs as (here
`1000:1000`); Docker must not create them, or they belong to root:

```
mkdir -p scans config
```

`compose.yml`:

```yaml
services:
  scan-watch:
    image: dartcafedocker/scan-watch:1
    container_name: scan-watch
    restart: unless-stopped
    init: true
    user: "1000:1000"
    ports:
      - "8080:8080"
    volumes:
      - ./scans:/scans
      - ./config:/config
    environment:
      SCAN_WATCH_HOST: 192.168.1.50            # scanner; or leave out and set it in the web interface
      SCAN_WATCH_PAPERLESS_URL: http://paperless:8000
      SCAN_WATCH_PAPERLESS_TOKEN: ${SCAN_WATCH_PAPERLESS_TOKEN}
      SCAN_WATCH_WEB_PASSWORD: ${SCAN_WATCH_WEB_PASSWORD}
      TZ: Europe/Berlin                         # used for the file names
    # Let a stack that is being scanned finish before the container stops.
    stop_grace_period: 3m
    read_only: true
    tmpfs:
      - /tmp
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
```

Put the token and the password into a `.env` file next to it, then:

```
docker compose up -d
docker compose logs -f
```

The web interface is at `http://<host>:8080`. Scan settings, target (folder,
Paperless or both) and tags are chosen there.

Or with `docker run`:

```
docker run -d --name scan-watch --init --user 1000:1000 -p 8080:8080 \
  -v "$PWD/scans:/scans" -v "$PWD/config:/config" \
  -e SCAN_WATCH_HOST=192.168.1.50 -e TZ=Europe/Berlin \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true \
  --stop-timeout 180 dartcafedocker/scan-watch:1
```

## Container

| Path / port | Purpose                                                   |
| ----------- | --------------------------------------------------------- |
| `/scans`    | PDFs                                                      |
| `/config`   | settings and profiles (`settings.json`)                   |
| `8080`      | web interface; `/healthz` is used by the health check     |

Runs as any user given with `--user` / `user:`; both folders must be writable for
it. Only outgoing connections are needed (HTTP to the scanner, HTTP(S) to
Paperless), so the default bridge network is fine. `docker stop` lets a stack that
is currently being scanned finish first.

## Environment

Variables fix a setting: it is shown read-only in the web interface and not
changed by profiles. Set only the installation here and choose the scan settings
in the web interface.

| Variable                          | Purpose                                                                 |
| --------------------------------- | ----------------------------------------------------------------------- |
| `SCAN_WATCH_HOST`                 | scanner IP or host name; without it, set in the web interface           |
| `SCAN_WATCH_PAPERLESS_URL`        | Paperless-ngx address as scan-watch reaches it                          |
| `SCAN_WATCH_PAPERLESS_TOKEN`      | API token (Paperless: profile menu → API Auth Token)                    |
| `SCAN_WATCH_PAPERLESS_PUBLIC_URL` | address for links in the browser, if it differs from the one above      |
| `SCAN_WATCH_PAPERLESS_TLS_PIN`    | fingerprint of a self-signed certificate (or pin it in the web interface) |
| `SCAN_WATCH_WEB_PASSWORD`         | password for the web interface (basic auth, any user name)              |
| `SCAN_WATCH_AUTO_PAUSE`           | minutes without paper until it pauses so the scanner can sleep (default 30, `0` = never) |
| `SCAN_WATCH_SNMP_COMMUNITY`       | Canon DR-S150 only, experimental: ends the automatic pause when paper is inserted |
| `TZ`                              | time zone for the file names                                            |

Without `SCAN_WATCH_WEB_PASSWORD`, everyone who can reach the port can change the
settings. Use a reverse proxy for HTTPS beyond the home network.

All variables (scan settings too) are listed in the
[README](https://github.com/dartcafe/scan-watch#settings).

## License

MIT
