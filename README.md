# whatsapp-merger

Merge two large WhatsApp Android `msgstore.db` backups into a single
deduplicated SQLite database.

See [INVESTIGATION.md](INVESTIGATION.md) for the feasibility analysis,
schema notes, and known limitations.

## Install

```bash
pip install -e .                # core merger only (works on plaintext .db)
pip install -e .[crypto]        # adds wa-crypt-tools for crypt12/14/15
```

## Use

Plaintext inputs:

```bash
whatsapp-merger A/msgstore.db B/msgstore.db -o merged.db
```

Encrypted inputs (key file or 64-character hex key):

```bash
whatsapp-merger \
    A/msgstore.db.crypt15 B/msgstore.db.crypt15 \
    --key-a A/encrypted_backup.key \
    --key-b B/encrypted_backup.key \
    -o merged.db
```

By default the merger is **additive**: rows already present in A are kept
unchanged. Pass `--prefer-newer` to overwrite destination fields with source
fields when the source row has a newer `received_timestamp`.

## How it works

The merger opens A as the destination, `ATTACH`es B as `src`, and copies in
dependency order: `jid` → `chat` → `message` → satellite tables
(`message_media`, `message_text`, …, `group_participant_user`). Cross-database
ID remapping is built in temp tables so very large backups stay inside
SQLite's streaming query path. Deduplication uses the schema's natural keys:

- modern: `(chat.jid_raw_string, message.from_me, message.key_id, sender.jid_raw_string)`
- legacy: `(messages.key_remote_jid, messages.key_from_me, messages.key_id)`

## Test

```bash
pytest
```

The tests build synthetic msgstore fixtures (no real WhatsApp data needed)
and exercise dedup, ID remapping, satellite-table preservation, the
quoted-message self-reference second pass, prefer-newer overwrite, and
legacy-schema merging.
