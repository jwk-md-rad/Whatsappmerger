# WhatsApp Backup Merge — Feasibility Investigation

## TL;DR

**Yes, merging two WhatsApp Android backups is feasible** when both can be
decrypted to plaintext SQLite (`msgstore.db`). It cannot be done generically on
encrypted blobs in Google Drive / iCloud, and it cannot reliably operate on
iOS+Android cross-platform pairs without one side being re-keyed first.

The merger reduces to a deduplicating, ID-remapping copy across two SQLite
schemas. The critical insight is that every WhatsApp Android schema since the
2022 refactor enforces a UNIQUE INDEX on
`(chat_row_id, from_me, key_id, sender_jid_row_id)` in the `message` table,
which gives us a deterministic deduplication key.

## Backup formats encountered

| Source | File on disk | Inner format | Notes |
|---|---|---|---|
| Android local | `msgstore.db.crypt12` | AES-GCM-256, key file | Pre-2021 |
| Android local | `msgstore.db.crypt14` | AES-GCM-256, key file (`/data/data/com.whatsapp/files/key`, 158 bytes) | Common |
| Android local + E2E | `msgstore.db.crypt15` | AES-GCM-256 derived from a 64-digit / 64-byte user key, plus a protobuf header | Required when "End-to-End Encrypted Backups" toggled on |
| Android Google Drive | proprietary chunked encryption | Re-encrypts on restore; only practically decryptable from a restored device | Not directly mergeable |
| iOS local (iTunes) | `ChatStorage.sqlite` (in `AppDomainGroup-group.net.whatsapp.WhatsApp.shared/`) | SQLite, *different* schema (`ZWAMESSAGE`, `ZWACHATSESSION`, `ZWAMEDIAITEM`) | Different schema; separate adapter required |
| iOS iCloud | encrypted CloudKit blobs | Not feasible without device | Not directly mergeable |

The plaintext output of `crypt14`/`crypt15` is `msgstore.db`, a regular SQLite
file. WhatsApp also ships separate `wa.db` (contacts), `axolotl.db` (Signal
keys), `chatsettings.db` etc. — for a "merge backup" the only file that needs
merging is `msgstore.db`. Media files (`/Media/...`) sit alongside, referenced
by paths inside `message_media.file_path`.

## Decryption (handled, not reinvented)

`crypt14` and `crypt15` are well-understood and the open-source `wa-crypt-tools`
package on PyPI implements both, including the protobuf header parsing for
crypt15 and the HKDF-SHA256 key expansion. We integrate it as an optional
dependency rather than rebuilding the cryptography ourselves — that's lower
risk and stays compatible with future format bumps.

If `wa-crypt-tools` isn't available at runtime, the CLI falls back to clear
instructions. If both inputs are already plaintext `.db` files, no
cryptography dependency is needed at all.

## Schema generations

WhatsApp's Android schema has gone through two major shapes:

**Legacy (pre-2022, "messages" table):** one fat table called `messages` with
columns like `key_remote_jid`, `key_from_me`, `key_id`, `data`, `media_*`, etc.
JIDs are stored as plain strings; chats are implied by `key_remote_jid`.

**Modern (2022+, "message" table):** normalized. `message` has
`_id, chat_row_id, from_me, key_id, sender_jid_row_id, timestamp,
message_type, text_data, ...`. Bodies are split across ~30 satellite tables:
`message_text`, `message_media`, `message_thumbnail`, `message_quoted`,
`message_link`, `message_vcard`, `message_location`, `message_system`,
`message_revoked`, `message_view_once`, `message_forwarded`,
`message_template`, `message_streaming_sidecar`, `message_payment`,
`message_orphaned_edit`, `message_ephemeral`, `message_future`,
`message_send_count`, `message_external_ad_content`, `message_mentions`,
`message_add_on`, `receipts`, `receipt_user`, `receipt_device`,
`receipt_orphaned`, `audio_data`, `frequents`, `group_participant_user`, …

`chat` references `jid` via `chat.jid_row_id → jid._id`. `message` references
both via `chat_row_id` and `sender_jid_row_id`.

WhatsApp adds and renames columns frequently. The merger must therefore
operate on the **intersection** of columns present in both source databases,
not a hard-coded list, or it will break the next release.

## Deduplication key

Empirically and per the schema's UNIQUE INDEX:

- **Modern:** `(chat_row_id, from_me, key_id, sender_jid_row_id)` — but
  `chat_row_id` and `sender_jid_row_id` are auto-increment IDs that differ
  between databases. So the cross-DB dedup key is really
  `(chat_jid_raw_string, from_me, key_id, sender_jid_raw_string)`.
- **Legacy:** `(key_remote_jid, key_from_me, key_id)`.

`key_id` is the WhatsApp message stanza ID. It is globally unique per sender,
which makes dedup robust even across very different time ranges.

## ID remapping at scale

The novel work in the merger is remapping autoincrement primary keys when
copying `B` into `A`. Approach:

1. `ATTACH DATABASE` the source onto the destination — single SQLite process,
   no Python data motion.
2. Build mapping tables in `TEMP` schema (`jid_map`, `chat_map`, `message_map`)
   keyed on `src_id` with `dst_id` value.
3. Insert in dependency order: jid → chat → message → satellite tables.
   Each insert uses the column intersection between source and destination.
4. For each non-trivial table, dedup against destination via natural key
   before insert, then populate the mapping.
5. Self-referential columns (`message.quoted_row_id`,
   `chat.last_message_row_id`, etc.) get a second-pass UPDATE after the
   message_map is fully populated.

This keeps everything inside SQLite's query planner, which streams rather than
materializing. Tested mentally against ~10 GB databases: the dominant cost is
the sequential scan of `message` (one pass for inserts, one for the mapping
build) plus index maintenance on the destination, not Python overhead.

## What the merger does NOT try to do

- **Reconcile media files.** The merger updates the media-row paths
  (`message_media.file_path` on Android, `ZWAMEDIAITEM.ZMEDIALOCALPATH` on
  iOS) and the inverse references, but does not copy the underlying image /
  video / audio blobs on disk. The operator must copy B's `Media/`
  directory alongside the merged DB. On iOS, iMazing's backup editor
  handles the `Manifest.db` rewrite for the file copies.
- **Cross-platform merge (Android ↔ iOS).** Schemas and message-type enums
  differ enough that this is a separate project.
- **Edit existing rows in A.** Default mode is strict-additive: rows that
  already exist in A are left alone. A `--prefer-newer` flag updates
  destination fields with non-null source fields where the source's
  `received_timestamp` is newer (Android only).
- **Re-encrypt the result.** Output is plaintext `msgstore.db` /
  `ChatStorage.sqlite`. Use `wa-crypt-tools` (Android) or iMazing (iOS) for
  re-injection.

## iOS (Core Data) adapter

The iOS merger handles the `ChatStorage.sqlite` layout: `ZWAMESSAGE`,
`ZWACHATSESSION`, `ZWAMEDIAITEM`, `ZWAGROUPMEMBER`, plus auto-discovered
satellite tables (`ZWAMESSAGEINFO`, `ZWAVCARDMENTION`, …). Cross-version
merges are supported (`--allow-model-hash-mismatch`); A must be the newer
WhatsApp build because the merged file inherits A's Core Data model.

Core-Data-specific responsibilities the merger takes on:

1. **Z_ENT remapping** — different WhatsApp versions assign different
   integer entity ids; we join `Z_PRIMARYKEY` on `Z_NAME` to translate.
2. **Z_PRIMARYKEY.Z_MAX bumps** — after every insert, set `Z_MAX` to the
   actual `MAX(Z_PK)` per touched entity, otherwise Core Data hands out
   colliding `Z_PK` values on the next save.
3. **Inverse 1-to-1 references** — `ZWAMEDIAITEM.ZMESSAGE` is set on
   insert; the matching `ZWAMESSAGE.ZMEDIAITEM` is filled in a second pass.
4. **Denormalized chat fields** — `ZLASTMESSAGE`, `ZLASTMESSAGEDATE`, and
   `ZMESSAGECOUNTER` are recomputed per chat from the actual message rows.
5. **WAL checkpoint** — both stores' `-wal` sidecars are folded into the
   main file before `ATTACH`.
6. **Post-merge integrity sweep** — verifies `Z_MAX >= MAX(Z_PK)` per
   entity and that media / message foreign keys resolve.

Restoration is delegated to a tool like iMazing, which rewrites
`Manifest.db` and re-encrypts the iTunes backup with the user's backup
password. The merger only produces a clean `ChatStorage.sqlite`.

## Risk notes

- Restoring a hand-merged `msgstore.db` to a real WhatsApp install is **not
  officially supported**. Stock WhatsApp expects the DB it produces. There
  are forks (e.g. GBWhatsApp, WhatsApp Plus) and forensic workflows where
  this is routine. We document the risk and produce a backup before writing.
- Schema drift is the single largest correctness risk. The merger logs every
  table and column it skips and refuses to start if either DB declares a
  version it doesn't recognize *and* `--force` isn't set.
- `key_id` collisions across distinct senders are theoretically possible but
  the four-tuple unique key (which includes `sender_jid_row_id`) handles it.

## Sources

- [whatsapp-viewer schema reference](https://github.com/andreas-mausch/whatsapp-viewer/blob/master/data/msgstore.db.schema.sql)
- [New msgstore — Who 'Dis? (binaryhick)](https://thebinaryhick.blog/2022/06/09/new-msgstore-who-dis-a-look-at-an-updated-whatsapp-on-android/)
- [Forensic Analysis of WhatsApp Messenger on Android (Anglano, arXiv:1507.07739)](https://arxiv.org/pdf/1507.07739)
- [Belkasoft — Android WhatsApp Forensics, Part II: Analysis](https://belkasoft.com/android-whatsapp-forensics-analysis)
- [wa-crypt-tools](https://github.com/ElDavoo/wa-crypt-tools)
- [The Workings of WhatsApp's Backups (sneela)](https://snee.la/posts/the-workings-of-whatsapps-end-to-end-encrypted-backups/)
- [Magnet Forensics — WhatsApp Artifact Profile](https://www.magnetforensics.com/blog/artifact-profile-whatsapp-messenger/)
