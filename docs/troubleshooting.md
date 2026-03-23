# Troubleshooting

## `AUTHZ_DENIED`
- Cause: role or project scope mismatch in team mode.
- Fix: verify token role and `projects` list in `ARCHIVIST_SSE_TOKENS`.

## Invalid `ARCHIVIST_SSE_TOKENS`
- Cause: malformed or invalid token map.
- Fix: provide valid JSON object mapping token -> `{user_id, role, projects[]}`.
- Behavior: server now fails closed on invalid token config.

## `CONFLICT_ERROR`
- Cause: optimistic concurrency mismatch (`expected_version` stale).
- Fix: read latest node, reapply update with current version, or use conflict resolve flow.

## `EMBEDDING_DISABLED`
- Cause: embeddings disabled or unavailable.
- Fix: enable embedding config, then run `rebuild_embeddings` or `rebuild_index_and_embeddings`.

## Integrity Check Failure
- Cause: DB corruption or invalid snapshot.
- Fix:
```bash
python3 scripts/check_integrity.py --db .archivist/archivist.db --auto-restore
```

## Encryption Startup Failure
- Cause: `ARCHIVIST_DB_ENCRYPTION_KEY` is set but SQLCipher support is missing.
- Fix: install a SQLCipher-enabled SQLite build, or unset the encryption key.

## Snapshot Restore Failure
- Cause: bad snapshot file.
- Fix: pick an earlier snapshot and rerun:
```bash
python3 scripts/restore_snapshot.py --snapshot .archivist/snapshots/<snapshot>.db --db .archivist/archivist.db
```
