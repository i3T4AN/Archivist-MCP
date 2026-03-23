# Upgrade and Migration Guide

## Upgrade Steps
1. Pull latest source.
2. Run migrations:
```bash
python3 scripts/migrate.py --db .archivist/archivist.db
```
3. Run integrity check:
```bash
python3 scripts/check_integrity.py --db .archivist/archivist.db
```
4. Rebuild derived state after major upgrades:
```bash
python3 scripts/rebuild_index_and_embeddings.py --db .archivist/archivist.db --project-id <project-id> --root <repo-root>
```

## Backward Compatibility
- Existing core tables are migrated forward via ordered SQL migrations.
- Tool response envelope remains `trace_id/version/warnings` with stable error taxonomy.

## Rollback Guidance
- Restore from latest valid snapshot:
```bash
python3 scripts/restore_snapshot.py --snapshot .archivist/snapshots/<snapshot>.db --db .archivist/archivist.db
```
