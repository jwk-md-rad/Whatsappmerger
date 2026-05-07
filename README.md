# whatsapp-photos

A fully searchable, password-protectable photo archive built from a
WhatsApp iOS backup.

Input: an extracted `ChatStorage.sqlite` plus the `Message/Media/` tree
(both pulled from your iTunes/Finder backup with a tool like iMazing).

Output: one self-contained SQLite database with FTS5 full-text indexes
plus a small local web viewer with thumbnails, filters, and a lightbox.

## Try it out (no real data needed)

```bash
pip install -e .
wa-photos demo
```

Builds a synthetic 12-photo / 5-chat archive under
`~/.cache/whatsapp_photos/demo/` and starts the viewer at
`http://127.0.0.1:8765/`. Useful as a UI preview before you've extracted
your real backup, and as a smoke test of the install.

## End-to-end walkthrough

If you're starting from scratch — iPhone in hand, no archive yet — read
**[docs/QUICKSTART.pdf](docs/QUICKSTART.pdf)** (or the markdown source
[`docs/QUICKSTART.md`](docs/QUICKSTART.md)). It covers everything from
making the iPhone backup with iMazing through ingesting, password
setup, Tailscale install, and opening the archive on the phone.

To rebuild the PDF after editing the markdown:

```bash
pip install -e .[docs]
python docs/build_pdf.py
```

## Install

```bash
pip install -e .          # core
pip install -e .[heic]    # add HEIC support (Pillow plug-in)
pip install -e .[dev]     # tests
```

## Build the archive

```bash
wa-photos ingest \
    /path/to/ChatStorage.sqlite \
    /path/to/AppDomainGroup-group.net.whatsapp.WhatsApp.shared \
    -o photos.db
```

Pass `--password` to set a viewer password right after ingest, or do it
later with `wa-photos password set photos.db`.

The ingester skips rows whose underlying file is missing on disk or isn't
a recognised image format. It logs counts in the report.

## Browse

```bash
wa-photos serve photos.db                 # http://127.0.0.1:8765/
wa-photos serve photos.db --port 9000
```

If a password is set, the browser prompts via HTTP Basic Auth (any
username, the password you set).

### Access from your phone (anywhere) via Tailscale

The server speaks plain HTTP. On a hostile network the password would
travel in cleartext, so we use [Tailscale](https://tailscale.com/) (free
for personal use) to put the laptop and phone on a private encrypted
WireGuard overlay — no port-forwarding, no certificates, no public
exposure.

1. Install Tailscale on the laptop and on the phone, sign both into the
   same account.
2. On the laptop:
   ```bash
   wa-photos serve photos.db --host 0.0.0.0
   ```
3. The startup banner prints the URLs you can paste into the phone
   browser, including the Tailscale MagicDNS one. Pick the Tailscale
   URL — it works from any network.

The first time you visit, the phone's browser will prompt for the
password you set on the archive.

If you only need access while on the same WiFi as the laptop, skip
Tailscale and just use the LAN URL the banner prints.

## Search from the CLI

```bash
wa-photos search photos.db "pizza"
wa-photos search photos.db --sender 555@s.whatsapp.net --since 2024-01-01
wa-photos search photos.db --json | jq '.[].caption'
```

## Password management

```bash
wa-photos password status photos.db   # set | not set
wa-photos password set photos.db      # set or change
wa-photos password clear photos.db    # remove protection
```

The password is stored as a PBKDF2-HMAC-SHA256 hash with a random
per-database salt. It gates **web access**, not at-rest reading of the
`.db` file — anyone with read access to the file can still query it
directly via SQLite. For at-rest encryption use SQLCipher or filesystem
encryption underneath.

## Search axes

- **Free-text** over caption, sender name, chat name, filename (FTS5,
  `unicode61`, accent-folded). Highlights are returned as snippets.
- **Filters**: chat, sender, group/1-on-1, from-me, date range.
- **Order**: newest, oldest, or relevance (when a query is given).

OCR of text inside photos and visual / face similarity search are not
in scope for this version — they need bigger dependencies (Tesseract,
CLIP) and were the explicit "no" in the scoping pass.

## Test

```bash
pytest
```

24 tests covering ingest (skip-missing, metadata extraction, idempotency,
SHA-256, FTS build), search (text, sender, chat, date range, count,
sanitizer), auth (round-trip, change, clear, empty rejection), and the
web layer (index render, search/photo/thumb endpoints, chat & sender
listings, Basic Auth on/off).
