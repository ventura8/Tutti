# Backups

Tutti backs up itself (t246, `tutti/backup.py`): the config file and the state folder (the backlog, the scouts, users,
Chat, usage logs, the household's records) in one dated archive, `tutti-backup-YYYYMMDD-HHMMSS.tar.gz`.

## What goes in and what stays out

In: the config file and every file of the state folder. Out: work copies that are rebuilt (`patches/`, `tests/`,
`quiet/`), locks and temporary files, and every secret. Secrets live in their own files under `~/.config/tutti`
(`grok.env`, `home-assistant.env`...), never in the state folder, so no archive holds one; Tutti does not store or send
credentials.

## When and where

The dashboard backs up ten minutes after it starts and then every `backup.every_hours` (24 by default), while
`backup.enabled` is on (the default). Archives go to `backup.dest`: any folder you may write, a mounted NAS too; empty means
`~/.local/share/tutti/backups`. After a verified backup only the newest `backup.keep` (14) are kept. All four are on
Settings, under *Updates and backups*.

## Verified

Each archive carries `MANIFEST.json`: every file's size and SHA-256. Right after it is made, the archive is read back
whole and every file checked against it, as a test restore would. A backup that does not verify is reported and the older
ones are kept. The Dashboard's Self-test card shows the last backup: when, its size and "verified", or what failed.

## By hand

```bash
tutti backup                 # one now: made, verified, the oldest pruned
tutti backup list            # the archives, newest first
tutti backup verify [file]   # read one back against its checksums (default: the newest)
tutti backup restore <file> --yes
```

Restore checks the archive first and changes nothing if it does not verify. It unpacks into a staging folder (no path may
leave it), keeps the current state folder aside as `<state>.before-restore-<date>`, puts the archive's state folder in
its place, and replaces the config after a copy of it (`config.yaml.before-restore-<date>`). Stop the dashboard first
(`systemctl --user stop tutti-dashboard`) and start it after.

## Not yet

Encryption, deduplication, Google Drive and OneDrive as destinations, and import and export from the dashboard come
next (t246).
