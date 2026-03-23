# Reliability and Recovery Runbook (Section 8)

## Startup Safety
- On stdio/SSE startup, integrity checks run when `reliability.startup_integrity_check=true`.
- If corruption is detected and `reliability.auto_restore_on_corruption=true`, the server restores from the latest snapshot in `reliability.snapshot_dir`.
- If auto-restore is disabled (default), startup fails closed with an integrity error.

## Daily Snapshot Procedure
1. Create snapshot:
```bash
python3 scripts/create_snapshot.py --db .archivist/archivist.db --snapshot-dir .archivist/snapshots
```
2. Verify latest snapshot:
```bash
python3 scripts/check_integrity.py --db .archivist/snapshots/<snapshot-file>.db
```

## Restore Procedure
1. Stop writer processes.
2. Restore snapshot:
```bash
python3 scripts/restore_snapshot.py --snapshot .archivist/snapshots/<snapshot-file>.db --db .archivist/archivist.db
```
3. Re-run migrations and integrity check:
```bash
python3 scripts/migrate.py
python3 scripts/check_integrity.py --db .archivist/archivist.db
```
4. Rebuild derived state (index + embeddings):
```bash
python3 scripts/rebuild_index_and_embeddings.py --db .archivist/archivist.db --project-id <project-id> --root <repo-root>
```

## Failure Modes
- Missing snapshot: recovery cannot proceed automatically; operator must restore from external backup.
- Snapshot integrity failure: restore is blocked; choose an earlier snapshot.
- Embeddings unavailable: rebuild command completes index pass and reports `EMBEDDING_DISABLED` warning.
