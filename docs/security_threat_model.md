# Security Policy and Threat Model (Section 7)

## Scope
- Local-first Archivist MCP server with optional team-mode SSE transport.
- Threat model covers payload handling, storage, authz boundaries, and auditability.

## Controls Implemented
- Input hardening:
  - strict per-tool payload key allowlists
  - type and size limits for IDs, query text, observation text, metadata arrays, and list fan-out
  - normalization + sanitization of free-form text to reduce prompt/data-injection persistence
- Access control:
  - role matrix: `reader`, `writer`, `maintainer`, `admin`
  - project-scope enforcement for all team-mode requests
- Data protection:
  - optional at-rest encryption key path (`PRAGMA key`) for SQLCipher builds
  - fail-closed mode when encryption is required but unsupported
- Logging/audit hygiene:
  - sensitive-pattern redaction for logs and audit export payloads
  - immutable audit event export with integrity digest (`sha256`)
- Retention and purge:
  - configurable observation retention (`7|30|90|180` days)
  - purge job with triage safeguard for high-value observations
  - purge summary recorded as immutable audit event

## Main Threats and Mitigations
1. Prompt/data injection persistence
- Mitigation: sanitize and normalize text at ingress; reject malformed payload shapes.

2. Privilege misuse in team mode
- Mitigation: tool-level role checks and strict project scope.

3. Sensitive token leakage in logs/exports
- Mitigation: recursive redaction before log/error surfacing and compliance export.

4. Over-retention of low-value observations
- Mitigation: scheduled/manual purge path with configurable retention + triage safeguard.

5. At-rest disclosure from host compromise
- Mitigation: optional encryption key integration for SQLCipher builds, with required-encryption guard.

## Residual Risk
- SQLCipher encryption depends on SQLite build capability at runtime.
- Team-mode transport currently assumes trusted network placement unless fronted by TLS termination.
