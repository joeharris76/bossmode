# Registry migration recovery

Bossmode creates a recovery copy before it changes an existing registry schema. The copy is an
SQLite online backup, so it includes committed WAL content and represents one consistent database
snapshot.

Bossmode applies each pending schema transition in its own transaction and creates a backup of that
transition's exact `from_version`. If several old transitions are pending, each one gets a separate
retained backup before its SQL runs.

## Backup contract

For the standard registry, backups are stored under `.bossmode/backups/`. A version 6 backup has a
name like:

```text
control.db.schema-6.20260825T231500123456Z-a1b2c3d4.sha256-<64-hex-digest>.sqlite3
```

The directory mode is `0700` and each new backup is `0600`. Bossmode runs a full SQLite integrity
check, flushes the backup file, publishes it without overwriting an existing path, and flushes the
backup directory before applying migration SQL. The applied-migration ledger stores the relative
backup path and the same SHA-256 digest.

Bossmode does not prune migration backups. Keep them until the migrated registry has passed its
independent data-integrity gate and the operator has adopted a separate retention policy. Backups
remain outside Git because `.bossmode/` is ignored.

## Migration lineage

Each new migration has one permanent ID, one ordered `from_version` and `to_version` transition,
and a checksum derived from those fields and its canonical SQL. Do not reuse an ID, change its SQL,
or reorder an applied transition. Bossmode rejects missing, unknown, duplicate, or checksum-mismatched
lineage before it opens a write transaction.

Schema 7 introduces the ledger and the single `20260825_01_migration_durability` transition from
schema 6. A fresh schema 7 registry seeds the same logical lineage without a backup reference. The
migration adds no team, worker, cleanup, artifact-adoption, or reporting tables.

## Restore a backup

Use this procedure only after a migration error identifies a backup path and SHA-256 digest.

1. Stop every Bossmode supervisor, worker, and command that can write this registry. Check for open
   `control.db`, `control.db-wal`, and `control.db-shm` handles before continuing.
2. Preserve the failed `control.db` and any `control.db-wal` and `control.db-shm` sidecars under a
   new operator-owned recovery directory. Do not delete them.
3. Verify the named backup before replacing the registry. Substitute the exact path and digest from
   the error message or ledger:

   ```bash
   uv run python - BACKUP_PATH EXPECTED_SHA256 <<'PY'
   import hashlib
   import sqlite3
   import sys
   from pathlib import Path

   path = Path(sys.argv[1])
   expected = sys.argv[2]
   with path.open("rb") as stream:
       actual = hashlib.file_digest(stream, "sha256").hexdigest()
   if actual != expected:
       raise SystemExit(f"backup checksum mismatch: expected {expected}, got {actual}")
   uri = f"{path.resolve().as_uri()}?mode=ro"
   with sqlite3.connect(uri, uri=True) as connection:
       integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
       version = connection.execute("SELECT version FROM schema_meta").fetchone()[0]
   if integrity != "ok":
       raise SystemExit(f"backup integrity check failed: {integrity}")
   print(f"verified schema {version} backup {path}")
   PY
   ```

4. Copy the verified backup to a new `0600` temporary file in `.bossmode/`. Flush that file, replace
   `.bossmode/control.db` atomically, and flush the `.bossmode/` directory. The following procedure
   also moves the stopped database and any WAL/SHM sidecars into a new recovery directory. Substitute
   explicit paths; the recovery directory must not already exist.

   ```bash
   uv run python - BACKUP_PATH .bossmode/control.db RECOVERY_DIRECTORY <<'PY'
   import os
   import shutil
   import sys
   from pathlib import Path

   backup, database, recovery = map(Path, sys.argv[1:])
   recovery.mkdir(mode=0o700)
   for suffix in ("", "-wal", "-shm"):
       active = database.with_name(f"{database.name}{suffix}")
       if active.exists():
           os.replace(active, recovery / active.name)

   temporary = database.with_name(f".{database.name}.restore.partial")
   flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
   flags |= getattr(os, "O_NOFOLLOW", 0)
   descriptor = os.open(temporary, flags, 0o600)
   with backup.open("rb") as source, os.fdopen(descriptor, "wb") as destination:
       shutil.copyfileobj(source, destination)
       destination.flush()
       os.fsync(destination.fileno())
   os.replace(temporary, database)
   directory_descriptor = os.open(
       database.parent,
       os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
   )
   try:
       os.fsync(directory_descriptor)
   finally:
       os.close(directory_descriptor)
   PY
   ```

   Do not copy over a live database or leave old WAL/SHM sidecars beside the restored file.
5. Open the restored database read-only. Run `PRAGMA integrity_check`, confirm its schema version,
   and inspect representative task, run, turn, evaluation, and feedback records.
6. Start only the Bossmode version that supports the restored schema. The current version will try
   the pending migration again and create another retained backup, so diagnose the original failure
   before retrying.

If the backup checksum or integrity check fails, keep the failed registry and all backup artifacts
unchanged and escalate to the operator. Do not bypass lineage validation or edit `schema_meta`.
