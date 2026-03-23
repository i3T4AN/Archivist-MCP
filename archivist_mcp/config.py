"""Configuration loading with defaults and env overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class MemoryConfig:
    core_max_kb: int = 12


@dataclass(frozen=True)
class RetrievalConfig:
    top_k: int = 8
    fts_weight: float = 0.35
    vector_weight: float = 0.35
    graph_weight: float = 0.20
    recency_weight: float = 0.10


@dataclass(frozen=True)
class EmbeddingConfig:
    enabled: bool = True
    provider: str = "hash-local"
    model: str = "bge-small-en-v1.5"
    dimensions: int = 384
    offline_strict: bool = True


@dataclass(frozen=True)
class SecurityConfig:
    max_id_chars: int = 128
    max_query_chars: int = 512
    max_observation_chars: int = 8000
    edge_fanout_limit: int = 64
    metadata_items_limit: int = 64
    observation_retention_days: int = 30
    encryption_key: str | None = None
    encryption_required: bool = False


@dataclass(frozen=True)
class ReliabilityConfig:
    snapshot_dir: str = ".archivist/snapshots"
    auto_restore_on_corruption: bool = False
    startup_integrity_check: bool = True


@dataclass(frozen=True)
class RateLimitConfig:
    enabled: bool = True
    per_actor_per_minute: int = 1000


@dataclass(frozen=True)
class ObservabilityConfig:
    structured_logging: bool = True
    alert_enabled: bool = True
    alert_min_calls: int = 20
    alert_error_rate_threshold: float = 0.5
    alert_cooldown_seconds: int = 60


@dataclass(frozen=True)
class FeatureFlagsConfig:
    experimental_tools_enabled: bool = True
    disabled_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class TlsConfig:
    enabled: bool = False
    cert_file: str | None = None
    key_file: str | None = None


@dataclass(frozen=True)
class AppConfig:
    memory: MemoryConfig = MemoryConfig()
    retrieval: RetrievalConfig = RetrievalConfig()
    embedding: EmbeddingConfig = EmbeddingConfig()
    security: SecurityConfig = SecurityConfig()
    reliability: ReliabilityConfig = ReliabilityConfig()
    rate_limit: RateLimitConfig = RateLimitConfig()
    observability: ObservabilityConfig = ObservabilityConfig()
    feature_flags: FeatureFlagsConfig = FeatureFlagsConfig()
    tls: TlsConfig = TlsConfig()


def load_config() -> AppConfig:
    """Load config from ARCHIVIST_CONFIG_PATH or .archivist/config.toml."""
    path = Path(os.getenv("ARCHIVIST_CONFIG_PATH", ".archivist/config.toml"))
    core_max_kb = 12
    top_k = 8
    fts_weight = 0.35
    vector_weight = 0.35
    graph_weight = 0.20
    recency_weight = 0.10
    embedding_enabled = True
    embedding_provider = "hash-local"
    embedding_model = "bge-small-en-v1.5"
    embedding_dimensions = 384
    embedding_offline_strict = True
    max_id_chars = 128
    max_query_chars = 512
    max_observation_chars = 8000
    edge_fanout_limit = 64
    metadata_items_limit = 64
    observation_retention_days = 30
    encryption_key = os.getenv("ARCHIVIST_DB_ENCRYPTION_KEY")
    encryption_required = False
    snapshot_dir = ".archivist/snapshots"
    auto_restore_on_corruption = False
    startup_integrity_check = True
    rate_limit_enabled = True
    rate_limit_per_actor_per_minute = 1000
    structured_logging = True
    alert_enabled = True
    alert_min_calls = 20
    alert_error_rate_threshold = 0.5
    alert_cooldown_seconds = 60
    experimental_tools_enabled = True
    disabled_tools: tuple[str, ...] = ()
    tls_enabled = False
    tls_cert_file: str | None = None
    tls_key_file: str | None = None

    if path.exists():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        mem = data.get("memory", {})
        retrieval = data.get("retrieval", {})
        embedding = data.get("embedding", {})
        security = data.get("security", {})
        reliability = data.get("reliability", {})
        rate_limit = data.get("rate_limit", {})
        observability = data.get("observability", {})
        feature_flags = data.get("feature_flags", {})
        tls = data.get("tls", {})
        if isinstance(mem.get("core_max_kb"), int):
            core_max_kb = mem["core_max_kb"]
        if isinstance(retrieval.get("top_k"), int):
            top_k = retrieval["top_k"]
        if isinstance(retrieval.get("fts_weight"), (int, float)):
            fts_weight = float(retrieval["fts_weight"])
        if isinstance(retrieval.get("vector_weight"), (int, float)):
            vector_weight = float(retrieval["vector_weight"])
        if isinstance(retrieval.get("graph_weight"), (int, float)):
            graph_weight = float(retrieval["graph_weight"])
        if isinstance(retrieval.get("recency_weight"), (int, float)):
            recency_weight = float(retrieval["recency_weight"])

        if isinstance(embedding.get("enabled"), bool):
            embedding_enabled = embedding["enabled"]
        if isinstance(embedding.get("provider"), str):
            embedding_provider = embedding["provider"]
        if isinstance(embedding.get("model"), str):
            embedding_model = embedding["model"]
        if isinstance(embedding.get("dimensions"), int):
            embedding_dimensions = embedding["dimensions"]
        if isinstance(embedding.get("offline_strict"), bool):
            embedding_offline_strict = embedding["offline_strict"]
        if isinstance(security.get("max_id_chars"), int):
            max_id_chars = security["max_id_chars"]
        if isinstance(security.get("max_query_chars"), int):
            max_query_chars = security["max_query_chars"]
        if isinstance(security.get("max_observation_chars"), int):
            max_observation_chars = security["max_observation_chars"]
        if isinstance(security.get("edge_fanout_limit"), int):
            edge_fanout_limit = security["edge_fanout_limit"]
        if isinstance(security.get("metadata_items_limit"), int):
            metadata_items_limit = security["metadata_items_limit"]
        if isinstance(security.get("observation_retention_days"), int):
            observation_retention_days = security["observation_retention_days"]
        if isinstance(security.get("encryption_key"), str) and security.get("encryption_key"):
            encryption_key = security["encryption_key"]
        if isinstance(security.get("encryption_required"), bool):
            encryption_required = security["encryption_required"]
        if isinstance(reliability.get("snapshot_dir"), str):
            snapshot_dir = reliability["snapshot_dir"]
        if isinstance(reliability.get("auto_restore_on_corruption"), bool):
            auto_restore_on_corruption = reliability["auto_restore_on_corruption"]
        if isinstance(reliability.get("startup_integrity_check"), bool):
            startup_integrity_check = reliability["startup_integrity_check"]
        if isinstance(rate_limit.get("enabled"), bool):
            rate_limit_enabled = rate_limit["enabled"]
        if isinstance(rate_limit.get("per_actor_per_minute"), int):
            rate_limit_per_actor_per_minute = rate_limit["per_actor_per_minute"]
        if isinstance(observability.get("structured_logging"), bool):
            structured_logging = observability["structured_logging"]
        if isinstance(observability.get("alert_enabled"), bool):
            alert_enabled = observability["alert_enabled"]
        if isinstance(observability.get("alert_min_calls"), int):
            alert_min_calls = observability["alert_min_calls"]
        if isinstance(observability.get("alert_error_rate_threshold"), (int, float)):
            alert_error_rate_threshold = float(observability["alert_error_rate_threshold"])
        if isinstance(observability.get("alert_cooldown_seconds"), int):
            alert_cooldown_seconds = observability["alert_cooldown_seconds"]
        if isinstance(feature_flags.get("experimental_tools_enabled"), bool):
            experimental_tools_enabled = feature_flags["experimental_tools_enabled"]
        if isinstance(feature_flags.get("disabled_tools"), list) and all(
            isinstance(x, str) for x in feature_flags.get("disabled_tools")
        ):
            disabled_tools = tuple(feature_flags.get("disabled_tools"))
        if isinstance(tls.get("enabled"), bool):
            tls_enabled = tls["enabled"]
        if isinstance(tls.get("cert_file"), str) and tls.get("cert_file"):
            tls_cert_file = tls["cert_file"]
        if isinstance(tls.get("key_file"), str) and tls.get("key_file"):
            tls_key_file = tls["key_file"]

    if os.getenv("ARCHIVIST_CORE_MAX_KB"):
        try:
            core_max_kb = int(os.environ["ARCHIVIST_CORE_MAX_KB"])
        except ValueError:
            pass
    if os.getenv("ARCHIVIST_DISABLE_EMBEDDINGS", "").lower() == "true":
        embedding_enabled = False
    if os.getenv("ARCHIVIST_OBSERVATION_RETENTION_DAYS"):
        try:
            observation_retention_days = int(os.environ["ARCHIVIST_OBSERVATION_RETENTION_DAYS"])
        except ValueError:
            pass
    if os.getenv("ARCHIVIST_ENCRYPTION_REQUIRED", "").lower() == "true":
        encryption_required = True
    if os.getenv("ARCHIVIST_AUTO_RESTORE_ON_CORRUPTION", "").lower() == "true":
        auto_restore_on_corruption = True
    if os.getenv("ARCHIVIST_RATE_LIMIT_ENABLED", "").lower() == "false":
        rate_limit_enabled = False
    if os.getenv("ARCHIVIST_RATE_LIMIT_PER_MINUTE"):
        try:
            rate_limit_per_actor_per_minute = int(os.environ["ARCHIVIST_RATE_LIMIT_PER_MINUTE"])
        except ValueError:
            pass
    if os.getenv("ARCHIVIST_STRUCTURED_LOGGING", "").lower() == "false":
        structured_logging = False
    if os.getenv("ARCHIVIST_ALERT_ENABLED", "").lower() == "false":
        alert_enabled = False
    if os.getenv("ARCHIVIST_ALERT_MIN_CALLS"):
        try:
            alert_min_calls = int(os.environ["ARCHIVIST_ALERT_MIN_CALLS"])
        except ValueError:
            pass
    if os.getenv("ARCHIVIST_ALERT_ERROR_RATE_THRESHOLD"):
        try:
            alert_error_rate_threshold = float(os.environ["ARCHIVIST_ALERT_ERROR_RATE_THRESHOLD"])
        except ValueError:
            pass
    if os.getenv("ARCHIVIST_ALERT_COOLDOWN_SECONDS"):
        try:
            alert_cooldown_seconds = int(os.environ["ARCHIVIST_ALERT_COOLDOWN_SECONDS"])
        except ValueError:
            pass
    if os.getenv("ARCHIVIST_EXPERIMENTAL_TOOLS_ENABLED", "").lower() == "false":
        experimental_tools_enabled = False
    if os.getenv("ARCHIVIST_DISABLED_TOOLS"):
        disabled_tools = tuple(
            part.strip()
            for part in os.environ["ARCHIVIST_DISABLED_TOOLS"].split(",")
            if part.strip()
        )
    if os.getenv("ARCHIVIST_TLS_ENABLED", "").lower() == "true":
        tls_enabled = True
    if os.getenv("ARCHIVIST_TLS_CERT_FILE"):
        tls_cert_file = os.environ["ARCHIVIST_TLS_CERT_FILE"]
    if os.getenv("ARCHIVIST_TLS_KEY_FILE"):
        tls_key_file = os.environ["ARCHIVIST_TLS_KEY_FILE"]

    if core_max_kb < 1:
        core_max_kb = 1
    if top_k < 1:
        top_k = 1
    if embedding_dimensions < 8:
        embedding_dimensions = 8
    if max_id_chars < 16:
        max_id_chars = 16
    if max_query_chars < 64:
        max_query_chars = 64
    if max_observation_chars < 256:
        max_observation_chars = 256
    if edge_fanout_limit < 1:
        edge_fanout_limit = 1
    if metadata_items_limit < 1:
        metadata_items_limit = 1
    if observation_retention_days not in {7, 30, 90, 180}:
        observation_retention_days = 30
    if rate_limit_per_actor_per_minute < 1:
        rate_limit_per_actor_per_minute = 1
    if alert_min_calls < 1:
        alert_min_calls = 1
    if alert_error_rate_threshold < 0.0:
        alert_error_rate_threshold = 0.0
    if alert_error_rate_threshold > 1.0:
        alert_error_rate_threshold = 1.0
    if alert_cooldown_seconds < 1:
        alert_cooldown_seconds = 1

    return AppConfig(
        memory=MemoryConfig(core_max_kb=core_max_kb),
        retrieval=RetrievalConfig(
            top_k=top_k,
            fts_weight=fts_weight,
            vector_weight=vector_weight,
            graph_weight=graph_weight,
            recency_weight=recency_weight,
        ),
        embedding=EmbeddingConfig(
            enabled=embedding_enabled,
            provider=embedding_provider,
            model=embedding_model,
            dimensions=embedding_dimensions,
            offline_strict=embedding_offline_strict,
        ),
        security=SecurityConfig(
            max_id_chars=max_id_chars,
            max_query_chars=max_query_chars,
            max_observation_chars=max_observation_chars,
            edge_fanout_limit=edge_fanout_limit,
            metadata_items_limit=metadata_items_limit,
            observation_retention_days=observation_retention_days,
            encryption_key=encryption_key,
            encryption_required=encryption_required,
        ),
        reliability=ReliabilityConfig(
            snapshot_dir=snapshot_dir,
            auto_restore_on_corruption=auto_restore_on_corruption,
            startup_integrity_check=startup_integrity_check,
        ),
        rate_limit=RateLimitConfig(
            enabled=rate_limit_enabled,
            per_actor_per_minute=rate_limit_per_actor_per_minute,
        ),
        observability=ObservabilityConfig(
            structured_logging=structured_logging,
            alert_enabled=alert_enabled,
            alert_min_calls=alert_min_calls,
            alert_error_rate_threshold=alert_error_rate_threshold,
            alert_cooldown_seconds=alert_cooldown_seconds,
        ),
        feature_flags=FeatureFlagsConfig(
            experimental_tools_enabled=experimental_tools_enabled,
            disabled_tools=disabled_tools,
        ),
        tls=TlsConfig(
            enabled=tls_enabled,
            cert_file=tls_cert_file,
            key_file=tls_key_file,
        ),
    )
