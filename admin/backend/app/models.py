"""
Database models for Athena Admin Interface.

These models represent the database schema for configuration management,
audit logging, device tracking, and user management.

DEV_MODE Compatibility:
    When DEV_MODE=true, SQLite is used instead of PostgreSQL. This module
    provides fallback types (JSON instead of JSONB, Text instead of ARRAY)
    to maintain compatibility.
"""
import hashlib
import hmac
import os
import secrets
from datetime import datetime
from typing import Dict, Any, Optional

from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Date, Text, ForeignKey, Index, UniqueConstraint, Float, Numeric, text, JSON
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

# Check for DEV_MODE to use compatible types
DEV_MODE = os.getenv("DEV_MODE", "false").lower() == "true"

if DEV_MODE:
    # SQLite-compatible types
    JSONB = JSON  # Use JSON instead of JSONB for SQLite
    # For ARRAY, we'll use a custom type that stores as JSON
    from sqlalchemy.types import TypeDecorator
    import json

    class ArrayType(TypeDecorator):
        """Custom ARRAY type that stores as JSON for SQLite compatibility."""
        impl = Text
        cache_ok = True

        def process_bind_param(self, value, dialect):
            if value is not None:
                return json.dumps(value)
            return None

        def process_result_value(self, value, dialect):
            if value is not None:
                return json.loads(value)
            return []

    # Replace ARRAY with ArrayType for DEV_MODE
    ARRAY = lambda t: ArrayType()
else:
    # PostgreSQL types for production
    from sqlalchemy.dialects.postgresql import JSONB, ARRAY

Base = declarative_base()


class User(Base):
    """User model for authentication and RBAC."""
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True)
    authentik_id = Column(String(255), unique=True, nullable=False, index=True)
    username = Column(String(255), unique=True, nullable=False, index=True)
    email = Column(String(255), unique=True, nullable=False)
    full_name = Column(String(255))
    role = Column(String(32), nullable=False, default='viewer')  # owner, operator, viewer, support
    active = Column(Boolean, default=True, nullable=False)
    last_login = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    policies_created = relationship('Policy', foreign_keys='Policy.created_by_id', back_populates='creator')
    audit_logs = relationship('AuditLog', back_populates='user')

    __table_args__ = (
        Index('idx_users_role', 'role'),
        Index('idx_users_active', 'active'),
    )

    def has_permission(self, permission: str) -> bool:
        """Check if user has a specific permission based on their role."""
        permissions = {
            'owner': {'read', 'write', 'delete', 'manage_users', 'manage_secrets', 'view_audit'},
            'operator': {'read', 'write', 'view_audit'},
            'viewer': {'read'},
            'support': {'read', 'view_audit'},
        }
        return permission in permissions.get(self.role, set())


class Policy(Base):
    """
    Policy model for storing orchestrator/RAG configuration.

    Supports both orchestrator modes (fast/medium/custom) and RAG configurations.
    Each policy change creates a new version for rollback capability.
    """
    __tablename__ = 'policies'

    id = Column(Integer, primary_key=True)
    mode = Column(String(16), nullable=False)  # 'fast', 'medium', 'custom', 'rag'
    config = Column(JSONB, nullable=False)  # Full configuration as JSON
    version = Column(Integer, nullable=False, default=1)
    created_by_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    active = Column(Boolean, default=True, nullable=False)
    description = Column(Text)

    # Relationships
    creator = relationship('User', foreign_keys=[created_by_id], back_populates='policies_created')
    versions = relationship('PolicyVersion', back_populates='policy', cascade='all, delete-orphan')
    audit_logs = relationship('AuditLog', back_populates='policy')

    __table_args__ = (
        Index('idx_policies_mode', 'mode'),
        Index('idx_policies_active', 'active'),
        Index('idx_policies_created_at', 'created_at'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert policy to dictionary for API responses."""
        return {
            'id': self.id,
            'mode': self.mode,
            'config': self.config,
            'version': self.version,
            'created_by': self.creator.username if self.creator else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'active': self.active,
            'description': self.description,
        }


class PolicyVersion(Base):
    """Version history for policy changes to support rollback."""
    __tablename__ = 'policy_versions'

    id = Column(Integer, primary_key=True)
    policy_id = Column(Integer, ForeignKey('policies.id'), nullable=False)
    version = Column(Integer, nullable=False)
    config = Column(JSONB, nullable=False)
    created_by_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    change_description = Column(Text)

    # Relationships
    policy = relationship('Policy', back_populates='versions')
    creator = relationship('User', foreign_keys=[created_by_id])

    __table_args__ = (
        UniqueConstraint('policy_id', 'version', name='uq_policy_version'),
        Index('idx_policy_versions_policy_id', 'policy_id'),
        Index('idx_policy_versions_created_at', 'created_at'),
    )


class Secret(Base):
    """
    Secret model for encrypted API keys and credentials.

    Stores encrypted secrets for services like OpenAI, weather APIs, etc.
    Uses application-level encryption before storage.
    """
    __tablename__ = 'secrets'

    id = Column(Integer, primary_key=True)
    service_name = Column(String(255), nullable=False, unique=True, index=True)
    encrypted_value = Column(Text, nullable=False)  # Application-encrypted
    description = Column(Text)
    created_by_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    last_rotated = Column(DateTime(timezone=True))

    # Relationships
    creator = relationship('User', foreign_keys=[created_by_id])
    audit_logs = relationship('AuditLog', back_populates='secret')

    __table_args__ = (
        Index('idx_secrets_service_name', 'service_name'),
        Index('idx_secrets_last_rotated', 'last_rotated'),
    )


class ExternalAPIKey(Base):
    """External API key storage with application-level encryption.

    Supports:
    - Basic API key authentication
    - OAuth 2.0 (client_id + client_secret)
    - Multiple keys per service (api_key, api_key2, api_key3)
    """
    __tablename__ = 'external_api_keys'

    id = Column(Integer, primary_key=True)
    service_name = Column(String(255), nullable=False, index=True)
    api_name = Column(String(255), nullable=False)

    # Primary API key (always required)
    api_key_encrypted = Column(Text, nullable=False)

    # OAuth 2.0 support (optional)
    client_id_encrypted = Column(Text, nullable=True)
    client_secret_encrypted = Column(Text, nullable=True)
    oauth_token_url = Column(Text, nullable=True)
    oauth_scopes = Column(Text, nullable=True)  # Comma-separated list

    # Multiple keys support (optional)
    key_type = Column(String(50), nullable=True)  # 'api_key', 'oauth', 'combined'
    key_purpose = Column(Text, nullable=True)  # Description of what this key is for
    api_key2_encrypted = Column(Text, nullable=True)
    api_key2_label = Column(String(100), nullable=True)
    api_key3_encrypted = Column(Text, nullable=True)
    api_key3_label = Column(String(100), nullable=True)

    # Additional configuration
    endpoint_url = Column(Text, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    description = Column(Text)
    rate_limit_per_minute = Column(Integer)
    extra_config = Column(JSONB, nullable=True)  # Flexible additional config

    # Audit fields
    created_by_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    last_used = Column(DateTime(timezone=True))

    creator = relationship('User', foreign_keys=[created_by_id])

    __table_args__ = (
        UniqueConstraint('service_name', 'key_type', name='uq_external_api_keys_service_key_type'),
        Index('idx_external_api_keys_service_name', 'service_name'),
        Index('idx_external_api_keys_enabled', 'enabled'),
        Index('idx_external_api_keys_last_used', 'last_used'),
        Index('idx_external_api_keys_key_type', 'key_type'),
    )


class UserAPIKey(Base):
    """
    Per-user API keys for programmatic access.

    Security:
    - Keys are hashed (SHA-256), never stored in recoverable form
    - Scopes limit permissions to subset of user's role
    - Keys can expire and be revoked
    - Usage tracked for monitoring
    """
    __tablename__ = 'user_api_keys'

    id = Column(Integer, primary_key=True)

    # Key ownership
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    name = Column(String(255), nullable=False)  # User-defined label

    # Key identification and verification
    key_prefix = Column(String(16), nullable=False, unique=True, index=True)  # First 16 chars for lookup
    key_hash = Column(String(64), nullable=False, unique=True)  # SHA-256 hash (64 hex chars)

    # Scoped permissions (subset of user permissions)
    scopes = Column(JSONB, nullable=False)  # ["read:devices", "write:features"]

    # Lifecycle
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)  # Optional expiration

    # Revocation (soft-delete for audit trail)
    revoked = Column(Boolean, default=False, nullable=False, index=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoked_reason = Column(Text, nullable=True)

    # Usage tracking
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    last_used_ip = Column(String(45), nullable=True)  # IPv6-compatible
    request_count = Column(Integer, default=0, nullable=False)

    # Audit
    created_by_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    created_reason = Column(Text, nullable=True)

    # Relationships
    user = relationship('User', foreign_keys=[user_id], backref='api_keys')
    creator = relationship('User', foreign_keys=[created_by_id])

    __table_args__ = (
        Index('idx_user_api_keys_user_id', 'user_id'),
        Index('idx_user_api_keys_key_prefix', 'key_prefix'),
        Index('idx_user_api_keys_revoked', 'revoked'),
        Index('idx_user_api_keys_expires_at', 'expires_at'),
        Index('idx_user_api_keys_last_used_at', 'last_used_at'),
    )

    def is_valid(self) -> bool:
        """Check if key is currently usable."""
        if self.revoked:
            return False
        if self.expires_at:
            # Handle both timezone-aware and naive datetimes
            now = datetime.utcnow()
            expires = self.expires_at
            if expires.tzinfo is not None:
                expires = expires.replace(tzinfo=None)
            if now > expires:
                return False
        return True

    def has_scope(self, required_scope: str) -> bool:
        """
        Check if key has required scope.

        Supports wildcards: 'read:*' matches 'read:devices', 'read:features', etc.
        """
        if not self.scopes:
            return False
        for scope in self.scopes:
            if scope == required_scope:
                return True
            if scope.endswith(':*') and required_scope.startswith(scope[:-1]):
                return True
        return False


class Device(Base):
    """
    Device model for tracking voice devices and services.

    Tracks Wyoming devices, jetson units, and other hardware in the system.
    """
    __tablename__ = 'devices'

    id = Column(Integer, primary_key=True)
    device_type = Column(String(32), nullable=False)  # 'wyoming', 'jetson', 'service'
    name = Column(String(255), nullable=False, unique=True, index=True)
    hostname = Column(String(255))
    ip_address = Column(String(45))  # IPv6-compatible
    port = Column(Integer)
    zone = Column(String(255))  # Physical location (e.g., 'office', 'kitchen')
    status = Column(String(32), default='unknown')  # 'online', 'offline', 'degraded', 'unknown'
    last_seen = Column(DateTime(timezone=True))
    config = Column(JSONB)  # Device-specific configuration
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    audit_logs = relationship('AuditLog', back_populates='device')

    __table_args__ = (
        Index('idx_devices_type', 'device_type'),
        Index('idx_devices_status', 'status'),
        Index('idx_devices_zone', 'zone'),
        Index('idx_devices_last_seen', 'last_seen'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert device to dictionary for API responses."""
        return {
            'id': self.id,
            'device_type': self.device_type,
            'name': self.name,
            'hostname': self.hostname,
            'ip_address': self.ip_address,
            'port': self.port,
            'zone': self.zone,
            'status': self.status,
            'last_seen': self.last_seen.isoformat() if self.last_seen else None,
            'config': self.config,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class AuditLog(Base):
    """
    Audit log for all configuration changes and sensitive operations.

    Provides tamper-evident logging using HMAC signatures.
    Immutable records for compliance and security.
    """
    __tablename__ = 'audit_logs'

    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    action = Column(String(64), nullable=False)  # 'create', 'update', 'delete', 'view', etc.
    resource_type = Column(String(64), nullable=False)  # 'policy', 'secret', 'device', etc.
    resource_id = Column(Integer)  # ID of the affected resource
    old_value = Column(JSONB)  # Previous state (for updates/deletes)
    new_value = Column(JSONB)  # New state (for creates/updates)
    ip_address = Column(String(45))
    user_agent = Column(Text)
    success = Column(Boolean, nullable=False, default=True)
    error_message = Column(Text)
    signature = Column(String(128))  # HMAC signature for tamper detection

    # Foreign key relationships (optional, for easier queries)
    policy_id = Column(Integer, ForeignKey('policies.id'))
    secret_id = Column(Integer, ForeignKey('secrets.id'))
    device_id = Column(Integer, ForeignKey('devices.id'))

    # Relationships
    user = relationship('User', back_populates='audit_logs')
    policy = relationship('Policy', back_populates='audit_logs')
    secret = relationship('Secret', back_populates='audit_logs')
    device = relationship('Device', back_populates='audit_logs')

    __table_args__ = (
        Index('idx_audit_logs_timestamp', 'timestamp'),
        Index('idx_audit_logs_user_id', 'user_id'),
        Index('idx_audit_logs_action', 'action'),
        Index('idx_audit_logs_resource_type', 'resource_type'),
        Index('idx_audit_logs_resource_composite', 'resource_type', 'resource_id'),
    )

    def compute_signature(self, secret_key: str) -> str:
        """
        Compute HMAC signature for tamper detection.

        Args:
            secret_key: Secret key for HMAC computation

        Returns:
            Hex-encoded HMAC signature
        """
        message = f"{self.id}:{self.timestamp}:{self.user_id}:{self.action}:{self.resource_type}:{self.resource_id}"
        return hmac.new(
            secret_key.encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()

    def verify_signature(self, secret_key: str) -> bool:
        """
        Verify HMAC signature to detect tampering.

        Args:
            secret_key: Secret key for HMAC computation

        Returns:
            True if signature is valid, False otherwise
        """
        if not self.signature:
            return False
        expected_signature = self.compute_signature(secret_key)
        return hmac.compare_digest(self.signature, expected_signature)

    def to_dict(self) -> Dict[str, Any]:
        """Convert audit log to dictionary for API responses."""
        username = self.user.username if self.user else None
        return {
            'id': self.id,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'user': username,  # Keep for backwards compatibility
            'username': username,  # More intuitive field name
            'user_id': self.user_id,
            'action': self.action,
            'resource_type': self.resource_type,
            'resource_id': self.resource_id,
            'old_value': self.old_value,
            'new_value': self.new_value,
            'ip_address': self.ip_address,
            'user_agent': self.user_agent,
            'success': self.success,
            'error_message': self.error_message,
        }


class ServerConfig(Base):
    """
    Server configuration model for tracking compute nodes.

    Tracks Mac Studio, Mac mini, Home Assistant, and other servers in the system.
    """
    __tablename__ = 'server_configs'

    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False, unique=True, index=True)
    hostname = Column(String(128))
    ip_address = Column(String(15), nullable=False)
    role = Column(String(32))  # "compute", "storage", "integration", "orchestration"
    status = Column(String(16), default='unknown')  # online, offline, degraded, unknown
    config = Column(JSONB)  # Flexible JSON config (ssh_user, docker_enabled, etc.)
    last_checked = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    services = relationship('ServiceRegistry', back_populates='server', cascade='all, delete-orphan')

    __table_args__ = (
        Index('idx_server_configs_name', 'name'),
        Index('idx_server_configs_status', 'status'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert server config to dictionary for API responses."""
        return {
            'id': self.id,
            'name': self.name,
            'hostname': self.hostname,
            'ip_address': self.ip_address,
            'role': self.role,
            'status': self.status,
            'config': self.config,
            'last_checked': self.last_checked.isoformat() if self.last_checked else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class ServiceRegistry(Base):
    """
    Service registry for tracking all services across servers.

    Links services to their host servers and tracks health status.
    """
    __tablename__ = 'service_registry'

    id = Column(Integer, primary_key=True)
    server_id = Column(Integer, ForeignKey('server_configs.id'), nullable=False)
    service_name = Column(String(64), nullable=False)
    port = Column(Integer, nullable=False)
    health_endpoint = Column(String(256))  # "/health", "/api/health", etc.
    protocol = Column(String(8), default='http')  # http, https, tcp
    status = Column(String(16), default='unknown')
    last_response_time = Column(Integer)  # milliseconds
    last_checked = Column(DateTime(timezone=True))

    # Relationships
    server = relationship('ServerConfig', back_populates='services')
    rag_connectors = relationship('RAGConnector', back_populates='service')

    __table_args__ = (
        UniqueConstraint('server_id', 'service_name', 'port', name='uq_service_registry'),
        Index('idx_service_registry_server_id', 'server_id'),
        Index('idx_service_registry_status', 'status'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert service registry entry to dictionary for API responses."""
        return {
            'id': self.id,
            'server_id': self.server_id,
            'server_name': self.server.name if self.server else None,
            'ip_address': self.server.ip_address if self.server else None,
            'service_name': self.service_name,
            'port': self.port,
            'health_endpoint': self.health_endpoint,
            'protocol': self.protocol,
            'status': self.status,
            'last_response_time': self.last_response_time,
            'last_checked': self.last_checked.isoformat() if self.last_checked else None,
        }


class RAGConnector(Base):
    """
    RAG connector configuration for external data sources.

    Manages configuration for weather, airports, sports, and custom RAG connectors.
    """
    __tablename__ = 'rag_connectors'

    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False, unique=True, index=True)
    connector_type = Column(String(32), nullable=False)  # "external_api", "vector_db", "cache", "custom"
    service_id = Column(Integer, ForeignKey('service_registry.id'))
    enabled = Column(Boolean, default=True)
    config = Column(JSONB)  # Connector-specific config (API endpoints, parameters, etc.)
    cache_config = Column(JSONB)  # Cache settings (TTL, size limits, eviction policy)
    created_by_id = Column(Integer, ForeignKey('users.id'))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    service = relationship('ServiceRegistry', back_populates='rag_connectors')
    creator = relationship('User')
    stats = relationship('RAGStats', back_populates='connector', cascade='all, delete-orphan')

    __table_args__ = (
        Index('idx_rag_connectors_name', 'name'),
        Index('idx_rag_connectors_enabled', 'enabled'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert RAG connector to dictionary for API responses."""
        return {
            'id': self.id,
            'name': self.name,
            'connector_type': self.connector_type,
            'service_id': self.service_id,
            'service_name': self.service.service_name if self.service else None,
            'enabled': self.enabled,
            'config': self.config,
            'cache_config': self.cache_config,
            'created_by': self.creator.username if self.creator else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class RAGStats(Base):
    """
    Statistics tracking for RAG connectors.

    Records usage metrics, cache performance, and errors for monitoring.
    """
    __tablename__ = 'rag_stats'

    id = Column(Integer, primary_key=True)
    connector_id = Column(Integer, ForeignKey('rag_connectors.id'), nullable=False)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    requests_count = Column(Integer, default=0)
    cache_hits = Column(Integer, default=0)
    cache_misses = Column(Integer, default=0)
    avg_response_time = Column(Integer)  # milliseconds
    error_count = Column(Integer, default=0)

    # Relationships
    connector = relationship('RAGConnector', back_populates='stats')

    __table_args__ = (
        Index('idx_rag_stats_connector_id', 'connector_id'),
        Index('idx_rag_stats_timestamp', 'timestamp'),
    )


class VoiceTest(Base):
    """
    Voice testing results storage.

    Stores test results for STT, TTS, LLM, RAG queries, and full pipeline tests.
    """
    __tablename__ = 'voice_tests'

    id = Column(Integer, primary_key=True)
    test_type = Column(String(32), nullable=False)  # "stt", "tts", "llm", "full_pipeline", "rag_query"
    test_input = Column(Text)  # Audio file path, text query, prompt, etc.
    test_config = Column(JSONB)  # Test parameters (model, voice, threshold, etc.)
    result = Column(JSONB)  # Test results with timing, response, errors
    success = Column(Boolean, nullable=False)
    error_message = Column(Text)
    executed_by_id = Column(Integer, ForeignKey('users.id'))
    executed_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    # Relationships
    executor = relationship('User')
    feedback = relationship('VoiceTestFeedback', back_populates='test', cascade='all, delete-orphan')

    __table_args__ = (
        Index('idx_voice_tests_test_type', 'test_type'),
        Index('idx_voice_tests_success', 'success'),
        Index('idx_voice_tests_executed_at', 'executed_at'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert voice test to dictionary for API responses."""
        return {
            'id': self.id,
            'test_type': self.test_type,
            'test_input': self.test_input,
            'test_config': self.test_config,
            'result': self.result,
            'success': self.success,
            'error_message': self.error_message,
            'executed_by': self.executor.username if self.executor else None,
            'executed_at': self.executed_at.isoformat() if self.executed_at else None,
        }


class VoiceTestFeedback(Base):
    """
    User feedback on voice test results for active learning.

    Allows users to mark test responses as correct/incorrect to improve system quality.
    """
    __tablename__ = 'voice_test_feedback'

    id = Column(Integer, primary_key=True)
    test_id = Column(Integer, ForeignKey('voice_tests.id', ondelete='CASCADE'), nullable=False)
    feedback_type = Column(String(20), nullable=False)  # 'correct' or 'incorrect'
    query = Column(Text, nullable=False)  # Original query for reference
    response = Column(Text)  # LLM response that was marked
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    notes = Column(Text)  # Optional user notes
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    test = relationship('VoiceTest', back_populates='feedback')
    user = relationship('User')

    __table_args__ = (
        Index('idx_feedback_test_id', 'test_id'),
        Index('idx_feedback_type', 'feedback_type'),
        Index('idx_feedback_created_at', 'created_at'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert voice test feedback to dictionary for API responses."""
        return {
            'id': self.id,
            'test_id': self.test_id,
            'feedback_type': self.feedback_type,
            'query': self.query,
            'response': self.response,
            'user': self.user.username if self.user else None,
            'notes': self.notes,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class IntentCategory(Base):
    """
    Intent categories for organizing and configuring intent detection.

    Provides hierarchical organization of intents (e.g., control, query, rag).
    """
    __tablename__ = 'intent_categories'

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, unique=True, index=True)
    display_name = Column(String(200), nullable=False)
    description = Column(Text)
    parent_id = Column(Integer, ForeignKey('intent_categories.id'))
    enabled = Column(Boolean, default=True)
    priority = Column(Integer, default=100)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    parent = relationship('IntentCategory', remote_side=[id], backref='children')
    confidence_rules = relationship('ConfidenceScoreRule', back_populates='category', cascade='all, delete-orphan')
    enhancement_rules = relationship('ResponseEnhancementRule', back_populates='category', cascade='all, delete-orphan')

    __table_args__ = (
        Index('idx_intent_categories_enabled', 'enabled'),
        Index('idx_intent_categories_parent_id', 'parent_id'),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'name': self.name,
            'display_name': self.display_name,
            'description': self.description,
            'parent_id': self.parent_id,
            'enabled': self.enabled,
            'priority': self.priority,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class HallucinationCheck(Base):
    """
    Anti-hallucination validation rules.

    Defines validation checks to prevent AI from generating false information.
    """
    __tablename__ = 'hallucination_checks'

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, unique=True, index=True)
    display_name = Column(String(200), nullable=False)
    description = Column(Text)
    check_type = Column(String(50), nullable=False)  # 'required_elements', 'fact_checking', 'confidence_threshold', 'cross_validation'
    applies_to_categories = Column(ARRAY(String), default=[])  # Empty = all categories
    enabled = Column(Boolean, default=True)
    severity = Column(String(20), default='warning')  # 'error', 'warning', 'info'
    configuration = Column(JSONB, nullable=False)  # Flexible config for different check types
    error_message_template = Column(Text)
    auto_fix_enabled = Column(Boolean, default=False)
    auto_fix_prompt_template = Column(Text)
    require_cross_model_validation = Column(Boolean, default=False)
    confidence_threshold = Column(Float, default=0.7)
    priority = Column(Integer, default=100)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    created_by = Column(String(100))

    __table_args__ = (
        Index('idx_hallucination_checks_enabled', 'enabled'),
        Index('idx_hallucination_checks_categories', 'applies_to_categories'),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'name': self.name,
            'display_name': self.display_name,
            'description': self.description,
            'check_type': self.check_type,
            'applies_to_categories': self.applies_to_categories,
            'enabled': self.enabled,
            'severity': self.severity,
            'configuration': self.configuration,
            'error_message_template': self.error_message_template,
            'auto_fix_enabled': self.auto_fix_enabled,
            'auto_fix_prompt_template': self.auto_fix_prompt_template,
            'require_cross_model_validation': self.require_cross_model_validation,
            'confidence_threshold': self.confidence_threshold,
            'priority': self.priority,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'created_by': self.created_by,
        }


class CrossValidationModel(Base):
    """
    Cross-model validation configuration.

    Configures multiple models for ensemble validation to reduce hallucinations.
    """
    __tablename__ = 'cross_validation_models'

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, unique=True, index=True)
    model_id = Column(String(100), nullable=False)  # e.g., 'phi3:mini', 'llama3.1:8b-q4'
    model_type = Column(String(50), nullable=False)  # 'primary', 'validation', 'fallback'
    endpoint_url = Column(String(500))
    enabled = Column(Boolean, default=True)
    use_for_categories = Column(ARRAY(String), default=[])
    temperature = Column(Float, default=0.1)
    max_tokens = Column(Integer, default=200)
    timeout_seconds = Column(Integer, default=30)
    weight = Column(Float, default=1.0)  # Weight for ensemble validation
    min_confidence_required = Column(Float, default=0.5)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index('idx_cross_validation_enabled', 'enabled', 'model_type'),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'name': self.name,
            'model_id': self.model_id,
            'model_type': self.model_type,
            'endpoint_url': self.endpoint_url,
            'enabled': self.enabled,
            'use_for_categories': self.use_for_categories,
            'temperature': self.temperature,
            'max_tokens': self.max_tokens,
            'timeout_seconds': self.timeout_seconds,
            'weight': self.weight,
            'min_confidence_required': self.min_confidence_required,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class MultiIntentConfig(Base):
    """
    Multi-intent processing configuration.

    Controls how queries with multiple intents are parsed and processed.
    """
    __tablename__ = 'multi_intent_config'

    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean, default=True)
    max_intents_per_query = Column(Integer, default=3)
    separators = Column(ARRAY(String), default=[' and ', ' then ', ' also ', ', then ', '; '])
    context_preservation = Column(Boolean, default=True)  # Preserve context between split intents
    parallel_processing = Column(Boolean, default=False)  # Process intents in parallel vs sequential
    combination_strategy = Column(String(50), default='concatenate')  # 'concatenate', 'summarize', 'hierarchical'
    min_words_per_intent = Column(Integer, default=2)
    context_words_to_preserve = Column(ARRAY(String), default=[])  # Words to carry forward if missing
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'enabled': self.enabled,
            'max_intents_per_query': self.max_intents_per_query,
            'separators': self.separators,
            'context_preservation': self.context_preservation,
            'parallel_processing': self.parallel_processing,
            'combination_strategy': self.combination_strategy,
            'min_words_per_intent': self.min_words_per_intent,
            'context_words_to_preserve': self.context_words_to_preserve,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class IntentChainRule(Base):
    """
    Intent chain rules for multi-step operations.

    Defines sequences of intents triggered by patterns (e.g., "goodnight" routine).
    """
    __tablename__ = 'intent_chain_rules'

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, index=True)
    trigger_pattern = Column(String(500))  # Regex pattern that triggers this chain
    intent_sequence = Column(ARRAY(String), nullable=False)  # Ordered list of intents to execute
    enabled = Column(Boolean, default=True)
    description = Column(Text)
    examples = Column(ARRAY(String))
    require_all = Column(Boolean, default=False)  # Whether all intents in chain must succeed
    stop_on_error = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index('idx_chain_rules_enabled', 'enabled'),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'name': self.name,
            'trigger_pattern': self.trigger_pattern,
            'intent_sequence': self.intent_sequence,
            'enabled': self.enabled,
            'description': self.description,
            'examples': self.examples,
            'require_all': self.require_all,
            'stop_on_error': self.stop_on_error,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class ValidationTestScenario(Base):
    """
    Validation test scenarios for testing anti-hallucination checks.

    Stores test cases to verify validation rules work correctly.
    """
    __tablename__ = 'validation_test_scenarios'

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False, index=True)
    test_query = Column(Text, nullable=False)
    initial_response = Column(Text, nullable=False)
    expected_validation_result = Column(String(20))  # 'pass', 'fail', 'warning'
    expected_checks_triggered = Column(ARRAY(String))
    expected_final_response = Column(Text)
    category = Column(String(50))
    enabled = Column(Boolean, default=True)
    last_run_result = Column(JSONB)
    last_run_date = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index('idx_validation_scenarios_enabled', 'enabled'),
        Index('idx_validation_scenarios_category', 'category'),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'name': self.name,
            'test_query': self.test_query,
            'initial_response': self.initial_response,
            'expected_validation_result': self.expected_validation_result,
            'expected_checks_triggered': self.expected_checks_triggered,
            'expected_final_response': self.expected_final_response,
            'category': self.category,
            'enabled': self.enabled,
            'last_run_result': self.last_run_result,
            'last_run_date': self.last_run_date.isoformat() if self.last_run_date else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class ConfidenceScoreRule(Base):
    """
    Confidence score adjustment rules.

    Defines factors that boost or penalize confidence scores for intent classification.
    """
    __tablename__ = 'confidence_score_rules'

    id = Column(Integer, primary_key=True)
    category_id = Column(Integer, ForeignKey('intent_categories.id', ondelete='CASCADE'))
    factor_name = Column(String(100), nullable=False)  # 'pattern_match_count', 'entity_presence', 'query_length'
    factor_type = Column(String(50), nullable=False)  # 'boost', 'penalty', 'multiplier'
    condition = Column(JSONB)  # e.g., {"min_matches": 2, "required_entities": ["room", "device"]}
    adjustment_value = Column(Float, nullable=False)  # Amount to adjust confidence by
    max_impact = Column(Float, default=0.2)  # Maximum impact this rule can have
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    category = relationship('IntentCategory', back_populates='confidence_rules')

    __table_args__ = (
        Index('idx_confidence_rules_category', 'category_id', 'enabled'),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'category_id': self.category_id,
            'category_name': self.category.name if self.category else None,
            'factor_name': self.factor_name,
            'factor_type': self.factor_type,
            'condition': self.condition,
            'adjustment_value': self.adjustment_value,
            'max_impact': self.max_impact,
            'enabled': self.enabled,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class ResponseEnhancementRule(Base):
    """
    Response enhancement rules.

    Defines rules for enhancing AI responses with additional context or formatting.
    """
    __tablename__ = 'response_enhancement_rules'

    id = Column(Integer, primary_key=True)
    category_id = Column(Integer, ForeignKey('intent_categories.id', ondelete='CASCADE'))
    enhancement_type = Column(String(50), nullable=False)  # 'add_context', 'format_data', 'add_suggestions', 'clarify_ambiguity'
    trigger_condition = Column(JSONB)  # When to apply this enhancement
    enhancement_template = Column(Text)
    enabled = Column(Boolean, default=True)
    priority = Column(Integer, default=100)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    category = relationship('IntentCategory', back_populates='enhancement_rules')

    __table_args__ = (
        Index('idx_enhancement_rules_category', 'category_id', 'enabled'),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'category_id': self.category_id,
            'category_name': self.category.name if self.category else None,
            'enhancement_type': self.enhancement_type,
            'trigger_condition': self.trigger_condition,
            'enhancement_template': self.enhancement_template,
            'enabled': self.enabled,
            'priority': self.priority,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class ConversationSettings(Base):
    """
    Conversation context management settings.

    Global settings for conversation session management, history tracking,
    and context preservation between queries.
    """
    __tablename__ = 'conversation_settings'

    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean, nullable=False, default=True)
    use_context = Column(Boolean, nullable=False, default=True)
    max_messages = Column(Integer, nullable=False, default=20)
    timeout_seconds = Column(Integer, nullable=False, default=1800)  # 30 minutes
    cleanup_interval_seconds = Column(Integer, nullable=False, default=60)
    session_ttl_seconds = Column(Integer, nullable=False, default=3600)  # 1 hour
    max_llm_history_messages = Column(Integer, nullable=False, default=10)
    history_mode = Column(String(20), nullable=False, default='full')  # 'none', 'summarized', 'full'
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def to_dict(self) -> Dict[str, Any]:
        """Convert conversation settings to dictionary for API responses."""
        return {
            'id': self.id,
            'enabled': self.enabled,
            'use_context': self.use_context,
            'max_messages': self.max_messages,
            'timeout_seconds': self.timeout_seconds,
            'cleanup_interval_seconds': self.cleanup_interval_seconds,
            'session_ttl_seconds': self.session_ttl_seconds,
            'max_llm_history_messages': self.max_llm_history_messages,
            'history_mode': self.history_mode,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class ClarificationSettings(Base):
    """
    Global clarification system settings.

    Controls whether clarifying questions are enabled and global timeout values.
    """
    __tablename__ = 'clarification_settings'

    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean, nullable=False, default=True)
    timeout_seconds = Column(Integer, nullable=False, default=300)  # 5 minutes
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def to_dict(self) -> Dict[str, Any]:
        """Convert clarification settings to dictionary for API responses."""
        return {
            'id': self.id,
            'enabled': self.enabled,
            'timeout_seconds': self.timeout_seconds,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class ClarificationType(Base):
    """
    Individual clarification type configurations.

    Defines different types of clarifying questions (device, location, time, sports_team)
    with individual enable/disable controls and priority ordering.
    """
    __tablename__ = 'clarification_types'

    id = Column(Integer, primary_key=True)
    type = Column(String(50), nullable=False, unique=True, index=True)
    enabled = Column(Boolean, nullable=False, default=True)
    timeout_seconds = Column(Integer)  # Override global timeout if set
    priority = Column(Integer, nullable=False, default=0)
    description = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index('idx_clarification_types_enabled', 'enabled'),
        Index('idx_clarification_types_priority', 'priority'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert clarification type to dictionary for API responses."""
        return {
            'id': self.id,
            'type': self.type,
            'enabled': self.enabled,
            'timeout_seconds': self.timeout_seconds,
            'priority': self.priority,
            'description': self.description,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class SportsTeamDisambiguation(Base):
    """
    Sports team disambiguation rules.

    Maps ambiguous team names (Giants, Cardinals, etc.) to specific options
    with JSONB data containing full team information.
    """
    __tablename__ = 'sports_team_disambiguation'

    id = Column(Integer, primary_key=True)
    team_name = Column(String(100), nullable=False, index=True)
    requires_disambiguation = Column(Boolean, nullable=False, default=True)
    options = Column(JSONB, nullable=False)  # [{"id": "ny-giants", "label": "NY Giants (NFL)", "sport": "football"}]
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index('idx_sports_team_name', 'team_name'),
        Index('idx_sports_disambiguation_required', 'requires_disambiguation'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert sports team disambiguation to dictionary for API responses."""
        return {
            'id': self.id,
            'team_name': self.team_name,
            'requires_disambiguation': self.requires_disambiguation,
            'options': self.options,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class DeviceDisambiguationRule(Base):
    """
    Device disambiguation rules for Home Assistant devices.

    Defines when to ask clarifying questions for device types (lights, switches, etc.)
    based on number of matching entities.
    """
    __tablename__ = 'device_disambiguation_rules'

    id = Column(Integer, primary_key=True)
    device_type = Column(String(50), nullable=False, unique=True, index=True)
    requires_disambiguation = Column(Boolean, nullable=False, default=True)
    min_entities_for_clarification = Column(Integer, nullable=False, default=2)
    include_all_option = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index('idx_device_type_enabled', 'device_type', 'requires_disambiguation'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert device disambiguation rule to dictionary for API responses."""
        return {
            'id': self.id,
            'device_type': self.device_type,
            'requires_disambiguation': self.requires_disambiguation,
            'min_entities_for_clarification': self.min_entities_for_clarification,
            'include_all_option': self.include_all_option,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class ConversationAnalytics(Base):
    """
    Analytics event tracking for conversation features.

    Records events like session creation, follow-up detection, and clarification triggers
    for monitoring and optimization.
    """
    __tablename__ = 'conversation_analytics'

    id = Column(Integer, primary_key=True)
    session_id = Column(String(255), nullable=False, index=True)
    event_type = Column(String(50), nullable=False, index=True)
    event_metadata = Column('metadata', JSONB)  # Maps Python attr 'event_metadata' to DB column 'metadata'
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    __table_args__ = (
        Index('idx_analytics_event_type', 'event_type'),
        Index('idx_analytics_timestamp', 'timestamp'),
        Index('idx_analytics_session_id', 'session_id'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert conversation analytics to dictionary for API responses."""
        return {
            'id': self.id,
            'session_id': self.session_id,
            'event_type': self.event_type,
            'metadata': self.event_metadata,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
        }


class LLMBackend(Base):
    """
    LLM backend configuration for model routing.

    Supports per-model backend selection (Ollama, MLX, Auto) with performance
    tracking and runtime configuration. Enables hybrid deployment with multiple
    LLM backends running simultaneously.
    """
    __tablename__ = 'llm_backends'

    id = Column(Integer, primary_key=True)
    model_name = Column(String(255), unique=True, nullable=False, index=True)
    backend_type = Column(String(32), nullable=False)  # ollama, mlx, auto
    endpoint_url = Column(String(500), nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    priority = Column(Integer, default=100)  # Lower = higher priority for 'auto' mode

    # Performance tracking
    avg_tokens_per_sec = Column(Float)
    avg_latency_ms = Column(Float)
    total_requests = Column(Integer, default=0)
    total_errors = Column(Integer, default=0)

    # Configuration
    max_tokens = Column(Integer, default=2048)
    temperature_default = Column(Float, default=0.7)
    timeout_seconds = Column(Integer, default=60)
    keep_alive_seconds = Column(Integer, default=-1)  # -1 = forever, 0 = unload immediately, >0 = seconds

    # Metadata
    description = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    created_by_id = Column(Integer, ForeignKey('users.id'))

    # Relationships
    creator = relationship('User')

    __table_args__ = (
        Index('idx_llm_backends_enabled', 'enabled'),
        Index('idx_llm_backends_backend_type', 'backend_type'),
        Index('idx_llm_backends_model_name', 'model_name'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert LLM backend to dictionary for API responses."""
        return {
            'id': self.id,
            'model_name': self.model_name,
            'backend_type': self.backend_type,
            'endpoint_url': self.endpoint_url,
            'enabled': self.enabled,
            'priority': self.priority,
            'avg_tokens_per_sec': self.avg_tokens_per_sec,
            'avg_latency_ms': self.avg_latency_ms,
            'total_requests': self.total_requests,
            'total_errors': self.total_errors,
            'max_tokens': self.max_tokens,
            'temperature_default': self.temperature_default,
            'timeout_seconds': self.timeout_seconds,
            'keep_alive_seconds': self.keep_alive_seconds,
            'description': self.description,
            'created_by': self.creator.username if self.creator else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class Feature(Base):
    """
    System feature flags for performance tracking and optimization.

    Tracks individual features in the system (intent classification, RAG services,
    caching, etc.) with enable/disable state and latency contribution.
    """
    __tablename__ = 'features'

    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True, nullable=False, index=True)
    display_name = Column(String(200), nullable=False)
    description = Column(Text)
    category = Column(String(50), nullable=False, index=True)  # 'processing', 'rag', 'optimization', 'integration'
    enabled = Column(Boolean, default=True, nullable=False, index=True)

    # Performance impact
    avg_latency_ms = Column(Float)  # Average latency contribution
    hit_rate = Column(Float)  # For caching features

    # Configuration
    required = Column(Boolean, default=False)  # Cannot be disabled
    priority = Column(Integer, default=100)
    config = Column(JSONB, default=dict)  # Feature-specific configuration (added in migration 033)
    requires_restart = Column(Boolean, default=False)  # Requires service restart to take effect

    # Metadata
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index('idx_features_enabled', 'enabled'),
        Index('idx_features_category', 'category'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert feature to dictionary for API responses."""
        return {
            'id': self.id,
            'name': self.name,
            'display_name': self.display_name,
            'description': self.description,
            'category': self.category,
            'enabled': self.enabled,
            'avg_latency_ms': self.avg_latency_ms,
            'hit_rate': self.hit_rate,
            'required': self.required,
            'priority': self.priority,
            'config': self.config or {},
            'requires_restart': self.requires_restart,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class LLMPerformanceMetric(Base):
    """
    LLM performance metrics for monitoring and analysis.

    Stores detailed performance metrics for each LLM request including latency,
    token generation speed, and contextual information for debugging and optimization.
    Enables historical analysis and performance regression detection.
    """
    __tablename__ = 'llm_performance_metrics'

    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    model = Column(String(100), nullable=False, index=True)
    backend = Column(String(50), nullable=False, index=True)
    latency_seconds = Column(Numeric(8, 3), nullable=False)
    tokens_generated = Column(Integer, nullable=False)
    tokens_per_second = Column(Numeric(10, 2), nullable=False)

    # Component latencies (milliseconds)
    gateway_latency_ms = Column(Float)
    intent_classification_latency_ms = Column(Float)
    rag_lookup_latency_ms = Column(Float)
    llm_inference_latency_ms = Column(Float)
    response_assembly_latency_ms = Column(Float)
    cache_lookup_latency_ms = Column(Float)

    # Feature flags (JSONB for flexibility)
    features_enabled = Column(JSONB)  # {"intent_classification": true, "rag": true, "caching": false}

    # Optional context fields
    prompt_tokens = Column(Integer, nullable=True)
    request_id = Column(String(100), nullable=True, index=True)
    session_id = Column(String(100), nullable=True, index=True)
    user_id = Column(String(100), nullable=True)
    zone = Column(String(100), nullable=True)
    intent = Column(String(100), nullable=True, index=True)
    source = Column(String(50), nullable=True, index=True)  # admin_voice_test, gateway, orchestrator, rag_*
    stage = Column(String(50), nullable=True, index=True)  # classify, summarize, tool_selection, validation, synthesize, etc.

    __table_args__ = (
        Index('idx_llm_metrics_timestamp', 'timestamp'),
        Index('idx_llm_metrics_model', 'model'),
        Index('idx_llm_metrics_backend', 'backend'),
        Index('idx_llm_metrics_intent', 'intent'),
        Index('idx_llm_metrics_composite', 'timestamp', 'model', 'backend'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert LLM performance metric to dictionary for API responses."""
        return {
            'id': self.id,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'model': self.model,
            'backend': self.backend,
            'latency_seconds': float(self.latency_seconds) if self.latency_seconds else None,
            'gateway_latency_ms': self.gateway_latency_ms,
            'intent_classification_latency_ms': self.intent_classification_latency_ms,
            'rag_lookup_latency_ms': self.rag_lookup_latency_ms,
            'llm_inference_latency_ms': self.llm_inference_latency_ms,
            'response_assembly_latency_ms': self.response_assembly_latency_ms,
            'cache_lookup_latency_ms': self.cache_lookup_latency_ms,
            'features_enabled': self.features_enabled,
            'tokens_generated': self.tokens_generated,
            'tokens_per_second': float(self.tokens_per_second) if self.tokens_per_second else None,
            'prompt_tokens': self.prompt_tokens,
            'request_id': self.request_id,
            'session_id': self.session_id,
            'user_id': self.user_id,
            'zone': self.zone,
            'intent': self.intent,
            'source': self.source,
            'stage': self.stage,
        }


class IntentPattern(Base):
    """
    Intent classification patterns for configurable routing.

    Maps keywords to intent categories with confidence weights.
    Replaces hardcoded patterns in intent_classifier.py.
    """
    __tablename__ = 'intent_patterns'

    id = Column(Integer, primary_key=True)
    intent_category = Column(String(50), nullable=False, index=True)
    pattern_type = Column(String(50), nullable=False)  # e.g., "basic", "dimming", "temperature"
    keyword = Column(String(100), nullable=False, index=True)
    confidence_weight = Column(Float, nullable=False, default=1.0)
    enabled = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint('intent_category', 'pattern_type', 'keyword', name='uq_intent_pattern_keyword'),
        Index('idx_intent_patterns_category', 'intent_category'),
        Index('idx_intent_patterns_enabled', 'enabled'),
        Index('idx_intent_patterns_keyword', 'keyword'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert intent pattern to dictionary for API responses."""
        return {
            'id': self.id,
            'intent_category': self.intent_category,
            'pattern_type': self.pattern_type,
            'keyword': self.keyword,
            'confidence_weight': self.confidence_weight,
            'enabled': self.enabled,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class IntentRouting(Base):
    """
    Intent routing configuration.

    Defines how each intent category should be routed:
    - To RAG services (weather, sports, etc.)
    - To web search providers
    - To LLM for processing

    Replaces hardcoded RAG_INTENTS list in provider_router.py.
    """
    __tablename__ = 'intent_routing'

    id = Column(Integer, primary_key=True)
    intent_category = Column(String(50), nullable=False, unique=True, index=True)
    use_rag = Column(Boolean, nullable=False, default=False)
    rag_service_url = Column(String(255), nullable=True)  # e.g., "http://localhost:8010"
    use_web_search = Column(Boolean, nullable=False, default=False)
    use_llm = Column(Boolean, nullable=False, default=True)
    priority = Column(Integer, nullable=False, default=100, index=True)  # Higher = checked first
    enabled = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint('intent_category', name='uq_intent_routing_category'),
        Index('idx_intent_routing_category', 'intent_category'),
        Index('idx_intent_routing_enabled', 'enabled'),
        Index('idx_intent_routing_priority', 'priority'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert intent routing to dictionary for API responses."""
        return {
            'id': self.id,
            'intent_category': self.intent_category,
            'use_rag': self.use_rag,
            'rag_service_url': self.rag_service_url,
            'use_web_search': self.use_web_search,
            'use_llm': self.use_llm,
            'priority': self.priority,
            'enabled': self.enabled,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class ProviderRouting(Base):
    """
    Web search provider routing configuration.

    Defines provider priority for each intent category.
    Replaces hardcoded INTENT_PROVIDER_SETS in provider_router.py.
    """
    __tablename__ = 'provider_routing'

    id = Column(Integer, primary_key=True)
    intent_category = Column(String(50), nullable=False, index=True)
    provider_name = Column(String(50), nullable=False, index=True)  # e.g., "duckduckgo", "brave"
    priority = Column(Integer, nullable=False, index=True)  # 1 = first, 2 = second, etc.
    enabled = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint('intent_category', 'provider_name', name='uq_provider_routing_category_provider'),
        Index('idx_provider_routing_category', 'intent_category'),
        Index('idx_provider_routing_provider', 'provider_name'),
        Index('idx_provider_routing_enabled', 'enabled'),
        Index('idx_provider_routing_priority', 'priority'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert provider routing to dictionary for API responses."""
        return {
            'id': self.id,
            'intent_category': self.intent_category,
            'provider_name': self.provider_name,
            'priority': self.priority,
            'enabled': self.enabled,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


# ============================================================================
# Guest Mode Models (Phase 2)
# ============================================================================

class GuestModeConfig(Base):
    """
    Guest mode configuration for vacation rental properties.

    Stores calendar integration settings, permission scopes, and
    data retention policies for guest mode operation.
    """
    __tablename__ = 'guest_mode_config'

    id = Column(Integer, primary_key=True)

    # Enable/disable guest mode globally
    enabled = Column(Boolean, default=False, nullable=False)

    # Calendar Integration
    calendar_source = Column(String(50), default='ical')  # 'ical', 'hostaway', 'guesty'
    calendar_url = Column(String(500))  # iCal URL for Airbnb calendar
    calendar_poll_interval_minutes = Column(Integer, default=10)

    # Buffer Times (hours)
    buffer_before_checkin_hours = Column(Integer, default=2)
    buffer_after_checkout_hours = Column(Integer, default=1)

    # Owner Override
    owner_pin = Column(String(128))  # Hashed PIN for voice override
    override_timeout_minutes = Column(Integer, default=60)

    # Permission Scopes (JSON arrays)
    guest_allowed_intents = Column(ARRAY(String), default=[])
    guest_restricted_entities = Column(ARRAY(String), default=[])
    guest_allowed_domains = Column(ARRAY(String), default=[])

    # Rate Limiting
    max_queries_per_minute_guest = Column(Integer, default=10)
    max_queries_per_minute_owner = Column(Integer, default=100)

    # Data Retention
    guest_data_retention_hours = Column(Integer, default=24)
    auto_purge_enabled = Column(Boolean, default=True)

    # Additional flexible config
    config = Column(JSONB, default={})

    # Audit fields
    created_by_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    creator = relationship('User', foreign_keys=[created_by_id])

    __table_args__ = (
        Index('idx_guest_mode_enabled', 'enabled'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert guest mode config to dictionary for API responses."""
        return {
            'id': self.id,
            'enabled': self.enabled,
            'calendar_source': self.calendar_source,
            'calendar_url': self.calendar_url if self.calendar_url else None,
            'calendar_poll_interval_minutes': self.calendar_poll_interval_minutes,
            'buffer_before_checkin_hours': self.buffer_before_checkin_hours,
            'buffer_after_checkout_hours': self.buffer_after_checkout_hours,
            'guest_allowed_intents': self.guest_allowed_intents,
            'guest_restricted_entities': self.guest_restricted_entities,
            'guest_allowed_domains': self.guest_allowed_domains,
            'max_queries_per_minute_guest': self.max_queries_per_minute_guest,
            'max_queries_per_minute_owner': self.max_queries_per_minute_owner,
            'guest_data_retention_hours': self.guest_data_retention_hours,
            'auto_purge_enabled': self.auto_purge_enabled,
            'config': self.config,
            'created_by': self.creator.username if self.creator else None,
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat(),
        }


class CalendarSource(Base):
    """
    Calendar source configuration for iCal feed sync.

    Stores iCal feed URLs from various vacation rental platforms
    (Airbnb, VRBO, Lodgify, etc.) for automatic synchronization.
    Users can add, enable/disable, and configure sources via Admin UI.
    """
    __tablename__ = 'calendar_sources'

    id = Column(Integer, primary_key=True)

    # Source identification
    name = Column(String(100), nullable=False)  # User-friendly name (e.g., "Airbnb - Main Listing")
    source_type = Column(String(50), nullable=False)  # 'airbnb', 'vrbo', 'lodgify', 'generic_ical'
    ical_url = Column(Text, nullable=False)  # Full iCal URL with auth tokens

    # Sync configuration
    enabled = Column(Boolean, default=True, nullable=False)
    sync_interval_minutes = Column(Integer, default=30, nullable=False)  # How often to sync
    priority = Column(Integer, default=1, nullable=False)  # Higher = preferred source for conflicts

    # Status tracking
    last_sync_at = Column(DateTime(timezone=True), nullable=True)
    last_sync_status = Column(String(50), default='pending')  # 'success', 'failed', 'pending'
    last_sync_error = Column(Text, nullable=True)  # Error message if last sync failed
    last_event_count = Column(Integer, default=0)  # Number of events from last sync

    # Property check-in/check-out times (stored as "HH:MM" format, e.g., "16:00")
    # These are used when the source API only provides dates, not times
    default_checkin_time = Column(String(5), default='16:00')  # 4:00 PM default
    default_checkout_time = Column(String(5), default='11:00')  # 11:00 AM default

    # Metadata
    description = Column(Text, nullable=True)  # Optional notes about this source
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index('idx_calendar_source_enabled', 'enabled'),
        Index('idx_calendar_source_type', 'source_type'),
        Index('idx_calendar_source_last_sync', 'last_sync_at'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert calendar source to dictionary for API responses."""
        return {
            'id': self.id,
            'name': self.name,
            'source_type': self.source_type,
            'ical_url': self.ical_url,
            'enabled': self.enabled,
            'sync_interval_minutes': self.sync_interval_minutes,
            'priority': self.priority,
            'last_sync_at': self.last_sync_at.isoformat() if self.last_sync_at else None,
            'last_sync_status': self.last_sync_status,
            'last_sync_error': self.last_sync_error,
            'last_event_count': self.last_event_count,
            'default_checkin_time': self.default_checkin_time,
            'default_checkout_time': self.default_checkout_time,
            'description': self.description,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }

    def to_dict_safe(self) -> Dict[str, Any]:
        """Convert to dictionary with URL masked for display."""
        data = self.to_dict()
        # Mask URL for security - only show first 30 chars
        if data['ical_url'] and len(data['ical_url']) > 30:
            data['ical_url_masked'] = data['ical_url'][:30] + '...'
        else:
            data['ical_url_masked'] = data['ical_url']
        return data


class CalendarEvent(Base):
    """
    Calendar events from vacation rental calendar (Airbnb, Vrbo, etc.).

    Cached events from iCal feed or PMS webhooks.
    Supports both iCal-synced events and manual guest entries.
    """
    __tablename__ = 'calendar_events'

    id = Column(Integer, primary_key=True)

    # Event identification
    external_id = Column(String(255), unique=True, nullable=False)  # UID from iCal or manual_<uuid>
    source = Column(String(50), default='ical')  # 'ical', 'airbnb', 'vrbo', 'hostaway', 'manual'
    source_id = Column(Integer, ForeignKey('calendar_sources.id', ondelete='SET NULL'), nullable=True)  # Link to CalendarSource

    # Event details
    title = Column(String(255))
    checkin = Column(DateTime(timezone=True), nullable=False)
    checkout = Column(DateTime(timezone=True), nullable=False)
    guest_name = Column(String(255))  # May be redacted based on PMS settings
    guest_email = Column(String(255), nullable=True)  # Guest email address (optional)
    guest_phone = Column(String(50), nullable=True)   # Guest phone number (optional)
    notes = Column(Text)

    # Status
    status = Column(String(50), default='confirmed')  # 'confirmed', 'cancelled', 'pending'

    # Tracking
    created_by = Column(String(50), default='ical_sync')  # 'ical_sync' or 'manual'
    deleted_at = Column(DateTime(timezone=True), nullable=True)  # Soft delete timestamp
    is_test = Column(Boolean, default=False, nullable=False)  # Test mode data

    # Metadata
    synced_at = Column(DateTime(timezone=True), server_default=func.now())
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index('idx_calendar_checkin', 'checkin'),
        Index('idx_calendar_checkout', 'checkout'),
        Index('idx_calendar_status', 'status'),
        Index('idx_calendar_synced_at', 'synced_at'),
        Index('idx_calendar_deleted_at', 'deleted_at'),
        Index('idx_calendar_created_by', 'created_by'),
        Index('idx_calendar_source_id', 'source_id'),
        Index('idx_calendar_events_is_test', 'is_test'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert calendar event to dictionary for API responses."""
        return {
            'id': self.id,
            'external_id': self.external_id,
            'source': self.source,
            'source_id': self.source_id,
            'title': self.title,
            'checkin': self.checkin.isoformat() if self.checkin else None,
            'checkout': self.checkout.isoformat() if self.checkout else None,
            'guest_name': self.guest_name,
            'guest_email': self.guest_email,
            'guest_phone': self.guest_phone,
            'notes': self.notes,
            'status': self.status,
            'created_by': self.created_by,
            'deleted_at': self.deleted_at.isoformat() if self.deleted_at else None,
            'is_test': self.is_test,
            'synced_at': self.synced_at.isoformat() if self.synced_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class ModeOverride(Base):
    """
    Manual mode overrides (owner voice PIN activation).

    Tracks when owner manually switches to owner mode via voice PIN.
    """
    __tablename__ = 'mode_overrides'

    id = Column(Integer, primary_key=True)

    # Override details
    mode = Column(String(20), nullable=False)  # 'owner' or 'guest'
    activated_by = Column(String(50))  # 'voice_pin', 'admin_ui', 'api'
    activated_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True))  # Null = no expiration

    # Context
    voice_device_id = Column(String(100))  # Which device activated it
    ip_address = Column(String(50))

    # Audit
    deactivated_at = Column(DateTime(timezone=True))

    __table_args__ = (
        Index('idx_mode_override_active', 'activated_at', 'expires_at'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert mode override to dictionary for API responses."""
        return {
            'id': self.id,
            'mode': self.mode,
            'activated_by': self.activated_by,
            'activated_at': self.activated_at.isoformat(),
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'voice_device_id': self.voice_device_id,
            'ip_address': self.ip_address,
            'deactivated_at': self.deactivated_at.isoformat() if self.deactivated_at else None,
        }


# ============================================================================
# Tool Calling Models (Phase 0 - Hybrid RAG)
# ============================================================================

class ToolRegistry(Base):
    """
    Tool registry for LLM tool calling.

    Stores available tools (RAG services, control functions) with their
    OpenAI function calling schemas and enable/disable state.
    """
    __tablename__ = 'tool_registry'

    id = Column(Integer, primary_key=True)
    tool_name = Column(String(100), nullable=False, unique=True, index=True)
    display_name = Column(String(200), nullable=False)
    description = Column(Text, nullable=False)
    category = Column(String(50), nullable=False, index=True)  # 'rag', 'control', 'info'
    function_schema = Column(JSONB, nullable=False)  # OpenAI function calling schema
    enabled = Column(Boolean, nullable=False, default=True, index=True)
    guest_mode_allowed = Column(Boolean, nullable=False, default=False, index=True)
    requires_auth = Column(Boolean, nullable=False, default=False)
    rate_limit_per_minute = Column(Integer)
    timeout_seconds = Column(Integer, nullable=False, default=30)
    priority = Column(Integer, nullable=False, default=100)
    service_url = Column(String(500))  # RAG service endpoint (e.g., "http://localhost:8010")
    web_search_fallback_enabled = Column(Boolean, nullable=False, default=True)  # Fallback to web search on failure
    required_api_keys = Column(JSONB, default=list)  # Cached list of required API key service names

    # MCP Integration (added in migration 033)
    source = Column(String(20), default='static')  # 'static' (Admin UI), 'mcp' (discovered), 'legacy' (hardcoded)
    mcp_endpoint = Column(String(500))  # For MCP tools: the webhook/endpoint URL
    last_discovered_at = Column(DateTime(timezone=True))  # When MCP tool was last seen during discovery
    discovery_metadata = Column(JSONB, default=dict)  # Metadata from MCP discovery (input schema, etc.)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationship to API key requirements
    api_key_requirements = relationship("ToolApiKeyRequirement", back_populates="tool", cascade="all, delete-orphan")

    __table_args__ = (
        Index('idx_tool_registry_enabled', 'enabled'),
        Index('idx_tool_registry_category', 'category'),
        Index('idx_tool_registry_guest_mode', 'guest_mode_allowed'),
        Index('idx_tool_registry_source', 'source'),
    )

    def to_dict(self, include_api_keys: bool = False) -> Dict[str, Any]:
        """Convert tool registry to dictionary for API responses."""
        result = {
            'id': self.id,
            'tool_name': self.tool_name,
            'display_name': self.display_name,
            'description': self.description,
            'category': self.category,
            'function_schema': self.function_schema,
            'enabled': self.enabled,
            'guest_mode_allowed': self.guest_mode_allowed,
            'requires_auth': self.requires_auth,
            'rate_limit_per_minute': self.rate_limit_per_minute,
            'timeout_seconds': self.timeout_seconds,
            'priority': self.priority,
            'service_url': self.service_url,
            'web_search_fallback_enabled': self.web_search_fallback_enabled,
            'required_api_keys': self.required_api_keys or [],
            'source': self.source or 'static',
            'mcp_endpoint': self.mcp_endpoint,
            'last_discovered_at': self.last_discovered_at.isoformat() if self.last_discovered_at else None,
            'discovery_metadata': self.discovery_metadata or {},
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_api_keys and self.api_key_requirements:
            result['api_key_details'] = [req.to_dict() for req in self.api_key_requirements]
        return result

    def update_api_keys_cache(self):
        """Update the cached required_api_keys list from relationships."""
        self.required_api_keys = [req.api_key_service for req in self.api_key_requirements]


class ToolApiKeyRequirement(Base):
    """
    Junction table linking tools to their required API keys.

    Defines which external API keys are needed for each tool to function.
    Enables validation before tool execution and automatic key injection.
    """
    __tablename__ = 'tool_api_key_requirements'

    id = Column(Integer, primary_key=True)
    tool_id = Column(Integer, ForeignKey('tool_registry.id', ondelete='CASCADE'), nullable=False, index=True)
    api_key_service = Column(String(255), nullable=False, index=True)  # References external_api_keys.service_name
    is_required = Column(Boolean, nullable=False, default=True)  # Required vs optional
    inject_as = Column(String(100))  # Parameter name to inject key as (e.g., "api_key", "google_api_key")
    description = Column(Text)  # Why this key is needed
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationship back to tool
    tool = relationship("ToolRegistry", back_populates="api_key_requirements")

    __table_args__ = (
        UniqueConstraint('tool_id', 'api_key_service', name='uq_tool_api_key_requirement'),
        Index('idx_tool_api_key_tool_id', 'tool_id'),
        Index('idx_tool_api_key_service', 'api_key_service'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'tool_id': self.tool_id,
            'api_key_service': self.api_key_service,
            'is_required': self.is_required,
            'inject_as': self.inject_as,
            'description': self.description,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class ToolCallingSetting(Base):
    """
    Tool calling system settings (singleton table).

    Global configuration for LLM tool calling: which model to use, timeouts,
    parallel execution limits, fallback behavior, etc.
    """
    __tablename__ = 'tool_calling_settings'

    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean, nullable=False, default=True)
    llm_model = Column(String(100), nullable=False, default='gpt-4o-mini')
    llm_backend = Column(String(50), nullable=False, default='openai')  # 'openai' or 'ollama'
    max_parallel_tools = Column(Integer, nullable=False, default=3)
    tool_call_timeout_seconds = Column(Integer, nullable=False, default=30)
    temperature = Column(Float, nullable=False, default=0.1)
    max_tokens = Column(Integer, nullable=False, default=500)
    fallback_to_direct_llm = Column(Boolean, nullable=False, default=True)
    cache_results = Column(Boolean, nullable=False, default=True)
    cache_ttl_seconds = Column(Integer, nullable=False, default=300)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def to_dict(self) -> Dict[str, Any]:
        """Convert tool calling settings to dictionary for API responses."""
        return {
            'id': self.id,
            'enabled': self.enabled,
            'llm_model': self.llm_model,
            'llm_backend': self.llm_backend,
            'max_parallel_tools': self.max_parallel_tools,
            'tool_call_timeout_seconds': self.tool_call_timeout_seconds,
            'temperature': self.temperature,
            'max_tokens': self.max_tokens,
            'fallback_to_direct_llm': self.fallback_to_direct_llm,
            'cache_results': self.cache_results,
            'cache_ttl_seconds': self.cache_ttl_seconds,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class ToolCallingTrigger(Base):
    """
    Tool calling fallback triggers.

    Defines conditions that trigger fallback from pattern-based routing
    to LLM tool calling (e.g., low confidence, ambiguous intent, multi-domain).
    """
    __tablename__ = 'tool_calling_triggers'

    id = Column(Integer, primary_key=True)
    trigger_name = Column(String(100), nullable=False, unique=True, index=True)
    trigger_type = Column(String(50), nullable=False, index=True)  # 'confidence', 'intent', 'keywords', 'validation', 'empty_rag'
    enabled = Column(Boolean, nullable=False, default=True, index=True)
    priority = Column(Integer, nullable=False, default=100)
    config = Column(JSONB, nullable=False)  # Trigger-specific configuration
    description = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index('idx_tool_calling_triggers_enabled', 'enabled'),
        Index('idx_tool_calling_triggers_type', 'trigger_type'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert tool calling trigger to dictionary for API responses."""
        return {
            'id': self.id,
            'trigger_name': self.trigger_name,
            'trigger_type': self.trigger_type,
            'enabled': self.enabled,
            'priority': self.priority,
            'config': self.config,
            'description': self.description,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class ToolUsageMetric(Base):
    """
    Tool usage metrics for monitoring and analysis.

    Tracks individual tool calls with latency, success rate, and context
    for performance monitoring and debugging.
    """
    __tablename__ = 'tool_usage_metrics'

    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    tool_name = Column(String(100), nullable=False, index=True)
    success = Column(Boolean, nullable=False, index=True)
    latency_ms = Column(Integer, nullable=False)
    error_message = Column(Text)
    trigger_reason = Column(String(100))  # Which trigger fired
    intent = Column(String(100))
    confidence = Column(Float)
    guest_mode = Column(Boolean, nullable=False, default=False)
    request_id = Column(String(100))
    session_id = Column(String(100))

    __table_args__ = (
        Index('idx_tool_usage_timestamp', 'timestamp'),
        Index('idx_tool_usage_tool_name', 'tool_name'),
        Index('idx_tool_usage_success', 'success'),
        Index('idx_tool_usage_composite', 'timestamp', 'tool_name', 'success'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert tool usage metric to dictionary for API responses."""
        return {
            'id': self.id,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'tool_name': self.tool_name,
            'success': self.success,
            'latency_ms': self.latency_ms,
            'error_message': self.error_message,
            'trigger_reason': self.trigger_reason,
            'intent': self.intent,
            'confidence': self.confidence,
            'guest_mode': self.guest_mode,
            'request_id': self.request_id,
            'session_id': self.session_id,
        }


class BaseKnowledge(Base):
    """
    Base knowledge system for context-aware voice assistant responses.

    Stores property information, user mode context, default locations,
    and temporal data for personalized responses.
    """
    __tablename__ = 'base_knowledge'

    id = Column(Integer, primary_key=True, index=True)
    category = Column(String(50), nullable=False, index=True)  # 'property', 'location', 'user', 'temporal', 'general'
    key = Column(String(100), nullable=False)
    value = Column(Text, nullable=False)
    applies_to = Column(String(20), nullable=False, server_default='both', index=True)  # 'guest', 'owner', 'both'
    priority = Column(Integer, nullable=False, server_default='0')  # Higher = injected first
    extra_metadata = Column(JSONB, nullable=True)
    enabled = Column(Boolean, nullable=False, server_default='true', index=True)
    description = Column(String(255), nullable=True)
    created_at = Column(DateTime, server_default=text('CURRENT_TIMESTAMP'))
    updated_at = Column(DateTime, server_default=text('CURRENT_TIMESTAMP'), onupdate=text('CURRENT_TIMESTAMP'))

    __table_args__ = (
        UniqueConstraint('category', 'key', 'applies_to', name='uix_category_key_applies'),
    )

    def to_dict(self):
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'category': self.category,
            'key': self.key,
            'value': self.value,
            'applies_to': self.applies_to,
            'priority': self.priority,
            'extra_metadata': self.extra_metadata,
            'enabled': self.enabled,
            'description': self.description,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class ComponentModelAssignment(Base):
    """
    Maps system components to their assigned LLM models.

    Enables dynamic configuration of which LLM model handles each component
    (intent classification, tool calling, response synthesis, etc.) via the
    admin UI. Supports hot-swapping without service restart via cache TTL.
    """
    __tablename__ = 'component_model_assignments'

    id = Column(Integer, primary_key=True)
    component_name = Column(String(100), unique=True, nullable=False, index=True)
    display_name = Column(String(255), nullable=False)
    description = Column(Text)
    category = Column(String(50), nullable=False, default='orchestrator')  # orchestrator, validation, control

    # Model assignment
    model_name = Column(String(255), nullable=False)  # e.g., "qwen2.5:1.5b"
    backend_type = Column(String(32), nullable=False, default='ollama')  # ollama, mlx, auto

    # Configuration overrides (optional, uses model defaults if null)
    temperature = Column(Float)
    max_tokens = Column(Integer)
    timeout_seconds = Column(Integer)

    # Status
    enabled = Column(Boolean, nullable=False, default=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index('idx_component_model_category', 'category'),
        Index('idx_component_model_enabled', 'enabled'),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'component_name': self.component_name,
            'display_name': self.display_name,
            'description': self.description,
            'category': self.category,
            'model_name': self.model_name,
            'backend_type': self.backend_type,
            'temperature': self.temperature,
            'max_tokens': self.max_tokens,
            'timeout_seconds': self.timeout_seconds,
            'enabled': self.enabled,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class AthenaService(Base):
    """
    Tracks Athena services for control and monitoring.

    Enables service control (start/stop/restart) and health monitoring
    from the admin UI. Supports Docker containers and launchd services.
    """
    __tablename__ = 'athena_services'

    id = Column(Integer, primary_key=True)
    service_name = Column(String(100), unique=True, nullable=False, index=True)
    display_name = Column(String(255), nullable=False)
    description = Column(Text)
    service_type = Column(String(50), nullable=False)  # 'rag', 'core', 'llm', 'infrastructure'

    # Connection info
    host = Column(String(255), nullable=False)
    port = Column(Integer, nullable=False)
    health_endpoint = Column(String(255), default='/health')

    # Control info
    control_method = Column(String(50), nullable=False, default='docker')  # docker, launchd, ollama
    container_name = Column(String(255))  # For docker control

    # Status (updated by health checks)
    is_running = Column(Boolean, default=False)
    last_health_check = Column(DateTime(timezone=True))
    last_error = Column(Text)

    # Configuration
    auto_start = Column(Boolean, default=True)
    enabled = Column(Boolean, default=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index('idx_athena_services_type', 'service_type'),
        Index('idx_athena_services_running', 'is_running'),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'service_name': self.service_name,
            'display_name': self.display_name,
            'description': self.description,
            'service_type': self.service_type,
            'host': self.host,
            'port': self.port,
            'health_endpoint': self.health_endpoint,
            'control_method': self.control_method,
            'container_name': self.container_name,
            'is_running': self.is_running,
            'last_health_check': self.last_health_check.isoformat() if self.last_health_check else None,
            'last_error': self.last_error,
            'auto_start': self.auto_start,
            'enabled': self.enabled,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class SystemSetting(Base):
    """
    Key-value store for system-wide settings.

    Used for configuration that needs to be persisted and shared across services,
    such as LLM memory settings, feature flags, etc.
    """
    __tablename__ = 'system_settings'

    id = Column(Integer, primary_key=True)
    key = Column(String(255), unique=True, nullable=False, index=True)
    value = Column(Text, nullable=False)
    description = Column(Text)
    category = Column(String(100), default='general')  # general, performance, security, etc.
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index('idx_system_settings_category', 'category'),
    )


class GatewayConfig(Base):
    """
    Gateway service configuration - singleton table (id=1).

    Stores all configurable settings for the gateway service,
    allowing hot-reconfiguration via admin UI without service restart.
    """
    __tablename__ = 'gateway_config'

    id = Column(Integer, primary_key=True)

    # Service URLs
    orchestrator_url = Column(String(500), nullable=False, default='http://localhost:8001')
    ollama_fallback_url = Column(String(500), nullable=False, default='http://localhost:11434')

    # Intent Classification
    intent_model = Column(String(255), nullable=False, default='phi3:mini')
    intent_temperature = Column(Float, nullable=False, default=0.1)
    intent_max_tokens = Column(Integer, nullable=False, default=10)
    intent_timeout_seconds = Column(Integer, nullable=False, default=5)

    # Timeouts
    orchestrator_timeout_seconds = Column(Integer, nullable=False, default=60)

    # Session Management
    session_timeout_seconds = Column(Integer, nullable=False, default=300)
    session_max_age_seconds = Column(Integer, nullable=False, default=86400)
    session_cleanup_interval_seconds = Column(Integer, nullable=False, default=60)

    # Cache
    cache_ttl_seconds = Column(Integer, nullable=False, default=60)

    # Rate Limiting
    rate_limit_enabled = Column(Boolean, nullable=False, default=False)
    rate_limit_requests_per_minute = Column(Integer, nullable=False, default=60)

    # Circuit Breaker
    circuit_breaker_enabled = Column(Boolean, nullable=False, default=False)
    circuit_breaker_failure_threshold = Column(Integer, nullable=False, default=5)
    circuit_breaker_recovery_timeout_seconds = Column(Integer, nullable=False, default=30)

    # Metadata
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def to_dict(self) -> Dict[str, Any]:
        """Convert gateway config to dictionary for API responses."""
        return {
            'id': self.id,
            'orchestrator_url': self.orchestrator_url,
            'ollama_fallback_url': self.ollama_fallback_url,
            'intent_model': self.intent_model,
            'intent_temperature': self.intent_temperature,
            'intent_max_tokens': self.intent_max_tokens,
            'intent_timeout_seconds': self.intent_timeout_seconds,
            'orchestrator_timeout_seconds': self.orchestrator_timeout_seconds,
            'session_timeout_seconds': self.session_timeout_seconds,
            'session_max_age_seconds': self.session_max_age_seconds,
            'session_cleanup_interval_seconds': self.session_cleanup_interval_seconds,
            'cache_ttl_seconds': self.cache_ttl_seconds,
            'rate_limit_enabled': self.rate_limit_enabled,
            'rate_limit_requests_per_minute': self.rate_limit_requests_per_minute,
            'circuit_breaker_enabled': self.circuit_breaker_enabled,
            'circuit_breaker_failure_threshold': self.circuit_breaker_failure_threshold,
            'circuit_breaker_recovery_timeout_seconds': self.circuit_breaker_recovery_timeout_seconds,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


# =============================================================================
# Performance Presets
# =============================================================================


class PerformancePreset(Base):
    """
    Performance preset - bundles all performance-related settings.

    Allows users to save and quickly switch between different performance
    configurations (e.g., "Super Fast" vs "Maximum Accuracy").

    Settings include:
    - Gateway intent model and parameters
    - Orchestrator component models (6 models for complexity-based routing)
    - Conversation history settings
    - HA optimization feature flags
    """
    __tablename__ = 'performance_presets'

    id = Column(Integer, primary_key=True)

    # Identification
    name = Column(String(100), nullable=False, unique=True)
    description = Column(Text)

    # Ownership
    is_system = Column(Boolean, nullable=False, default=False)
    created_by_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)

    # Active state
    is_active = Column(Boolean, nullable=False, default=False)

    # Settings snapshot
    settings = Column(JSONB, nullable=False, default=dict)

    # Metadata
    estimated_latency_ms = Column(Integer)
    icon = Column(String(10))

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    created_by = relationship("User", foreign_keys=[created_by_id])

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'is_system': self.is_system,
            'is_active': self.is_active,
            'settings': self.settings,
            'estimated_latency_ms': self.estimated_latency_ms,
            'icon': self.icon,
            'created_by_id': self.created_by_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


# =============================================================================
# SMS Models
# =============================================================================


class SMSSettings(Base):
    """
    Global SMS feature settings (singleton table).

    Controls Twilio integration, rate limiting, and auto-offer behavior.
    """
    __tablename__ = 'sms_settings'

    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean, nullable=False, default=False)
    test_mode = Column(Boolean, nullable=False, default=True)
    auto_offer_mode = Column(String(20), nullable=False, default='smart')
    # auto_offer_mode: 'smart' (detect content), 'always', 'never'
    rate_limit_per_minute = Column(Integer, nullable=False, default=10)
    rate_limit_per_stay = Column(Integer, nullable=False, default=50)
    from_number = Column(String(20), nullable=True)  # Cached from external_api_keys
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def to_dict(self) -> Dict[str, Any]:
        """Convert settings to dictionary for API responses."""
        return {
            'id': self.id,
            'enabled': self.enabled,
            'test_mode': self.test_mode,
            'auto_offer_mode': self.auto_offer_mode,
            'rate_limit_per_minute': self.rate_limit_per_minute,
            'rate_limit_per_stay': self.rate_limit_per_stay,
            'from_number': self.from_number,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class GuestSMSPreference(Base):
    """
    Per-stay SMS preferences for guests.

    Tracks whether a guest wants SMS notifications and their "don't ask again" preference.
    Resets with each new stay.
    """
    __tablename__ = 'guest_sms_preferences'

    id = Column(Integer, primary_key=True)
    calendar_event_id = Column(Integer, ForeignKey('calendar_events.id', ondelete='CASCADE'), nullable=False)
    sms_enabled = Column(Boolean, nullable=False, default=True)
    dont_ask_again = Column(Boolean, nullable=False, default=False)
    preferred_phone = Column(String(50), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    calendar_event = relationship('CalendarEvent', backref='sms_preferences')

    __table_args__ = (
        Index('idx_guest_sms_prefs_event', 'calendar_event_id', unique=True),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert preferences to dictionary for API responses."""
        return {
            'id': self.id,
            'calendar_event_id': self.calendar_event_id,
            'sms_enabled': self.sms_enabled,
            'dont_ask_again': self.dont_ask_again,
            'preferred_phone': self.preferred_phone,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class SMSHistory(Base):
    """
    Log of all SMS messages sent.

    Tracks message content, delivery status, and Twilio details.
    """
    __tablename__ = 'sms_history'

    id = Column(Integer, primary_key=True)
    calendar_event_id = Column(Integer, ForeignKey('calendar_events.id', ondelete='SET NULL'), nullable=True)
    phone_number = Column(String(50), nullable=False)
    content = Column(Text, nullable=False)
    content_summary = Column(String(255), nullable=True)
    content_type = Column(String(50), nullable=True)  # 'wifi', 'address', 'link', 'custom'
    triggered_by = Column(String(50), nullable=True)  # 'user_request', 'auto_offer', 'scheduled', 'admin'
    original_query = Column(Text, nullable=True)
    session_id = Column(String(255), nullable=True)
    twilio_sid = Column(String(100), nullable=True)
    status = Column(String(20), nullable=False, default='queued')
    # status: 'queued', 'sent', 'delivered', 'failed', 'undelivered'
    error_code = Column(String(20), nullable=True)
    error_message = Column(Text, nullable=True)
    segment_count = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    delivered_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    calendar_event = relationship('CalendarEvent', backref='sms_messages')

    __table_args__ = (
        Index('idx_sms_history_event', 'calendar_event_id'),
        Index('idx_sms_history_created', 'created_at'),
        Index('idx_sms_history_status', 'status'),
        Index('idx_sms_history_type', 'content_type'),
        Index('idx_sms_history_phone', 'phone_number'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert history entry to dictionary for API responses."""
        return {
            'id': self.id,
            'calendar_event_id': self.calendar_event_id,
            'phone_number': self.phone_number,
            'content': self.content,
            'content_summary': self.content_summary,
            'content_type': self.content_type,
            'triggered_by': self.triggered_by,
            'original_query': self.original_query,
            'session_id': self.session_id,
            'twilio_sid': self.twilio_sid,
            'status': self.status,
            'error_code': self.error_code,
            'error_message': self.error_message,
            'segment_count': self.segment_count,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'sent_at': self.sent_at.isoformat() if self.sent_at else None,
            'delivered_at': self.delivered_at.isoformat() if self.delivered_at else None,
        }


class SMSCostTracking(Base):
    """
    Track SMS costs per stay and monthly.

    Either calendar_event_id (per-stay) or month (monthly aggregate) is set.
    """
    __tablename__ = 'sms_cost_tracking'

    id = Column(Integer, primary_key=True)
    calendar_event_id = Column(Integer, ForeignKey('calendar_events.id', ondelete='SET NULL'), nullable=True)
    month = Column(Date, nullable=True)  # First day of month for monthly aggregation
    message_count = Column(Integer, nullable=False, default=0)
    segment_count = Column(Integer, nullable=False, default=0)
    incoming_count = Column(Integer, nullable=False, default=0)
    outgoing_count = Column(Integer, nullable=False, default=0)
    estimated_cost_cents = Column(Integer, nullable=False, default=0)
    outgoing_sms_cents = Column(Integer, nullable=False, default=0)
    incoming_sms_cents = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    calendar_event = relationship('CalendarEvent', backref='sms_costs')

    __table_args__ = (
        Index('idx_sms_cost_event', 'calendar_event_id'),
        Index('idx_sms_cost_month', 'month'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert cost tracking to dictionary for API responses."""
        return {
            'id': self.id,
            'calendar_event_id': self.calendar_event_id,
            'month': self.month.isoformat() if self.month else None,
            'message_count': self.message_count,
            'segment_count': self.segment_count,
            'incoming_count': self.incoming_count,
            'outgoing_count': self.outgoing_count,
            'estimated_cost_cents': self.estimated_cost_cents,
            'cost_formatted': f"${self.estimated_cost_cents / 100:.2f}",
            'outgoing_sms_cents': self.outgoing_sms_cents,
            'incoming_sms_cents': self.incoming_sms_cents,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


# ============================================================================
# SMS Enhanced Features Models (Phase 2)
# ============================================================================

class TipPrompt(Base):
    """Configurable tips shown to guests during conversations."""
    __tablename__ = 'tip_prompts'

    id = Column(Integer, primary_key=True, autoincrement=True)
    tip_type = Column(String(50), nullable=False)  # 'sms_offer', 'feature_hint', 'local_tip'
    title = Column(String(100), nullable=False)
    message = Column(Text, nullable=False)
    trigger_condition = Column(String(100), nullable=True)  # 'after_wifi', 'first_question', etc.
    trigger_intent = Column(String(100), nullable=True)  # Specific intent to trigger on
    enabled = Column(Boolean, default=True, nullable=False)
    priority = Column(Integer, default=100, nullable=False)
    max_shows_per_stay = Column(Integer, default=1, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    history = relationship("TipPromptHistory", back_populates="tip", cascade="all, delete-orphan")

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'category': self.tip_type,  # Map tip_type to category for API
            'title': self.title,
            'content': self.message,  # Map message to content for API
            'trigger_condition': self.trigger_condition,
            'trigger_intent': self.trigger_intent,
            'enabled': self.enabled,
            'priority': self.priority,
            'max_shows_per_stay': self.max_shows_per_stay,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class TipPromptHistory(Base):
    """Tracks which tips have been shown to which guests."""
    __tablename__ = 'tip_prompt_history'

    id = Column(Integer, primary_key=True, autoincrement=True)
    tip_id = Column(Integer, ForeignKey('tip_prompts.id', ondelete='CASCADE'), nullable=False)
    calendar_event_id = Column(Integer, ForeignKey('calendar_events.id', ondelete='CASCADE'), nullable=True)
    session_id = Column(String(255), nullable=True)
    shown_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    accepted = Column(Boolean, nullable=True)  # Did guest act on the tip?

    # Relationships
    tip = relationship("TipPrompt", back_populates="history")
    calendar_event = relationship("CalendarEvent")

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'tip_id': self.tip_id,
            'calendar_event_id': self.calendar_event_id,
            'session_id': self.session_id,
            'shown_at': self.shown_at.isoformat() if self.shown_at else None,
            'accepted': self.accepted,
        }


class SMSTemplate(Base):
    """Templates for proactive SMS messages."""
    __tablename__ = 'sms_templates'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False, unique=True)
    category = Column(String(50), nullable=True)  # 'welcome', 'checkout', 'reminder', 'custom'
    subject = Column(String(100), nullable=True)
    body = Column(Text, nullable=False)
    variables = Column(JSONB, nullable=True)  # List of variable names
    enabled = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    scheduled_messages = relationship("ScheduledSMS", back_populates="template")

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'name': self.name,
            'category': self.category,
            'subject': self.subject,
            'body': self.body,
            'variables': self.variables or [],
            'enabled': self.enabled,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class ScheduledSMS(Base):
    """Scheduled/proactive SMS configurations."""
    __tablename__ = 'scheduled_sms'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    trigger_type = Column(String(50), nullable=False)  # 'before_checkin', 'after_checkin', 'before_checkout', 'time_of_day'
    trigger_offset_hours = Column(Integer, default=0, nullable=False)
    trigger_time = Column(DateTime, nullable=True)  # Specific time for time_of_day trigger
    template_id = Column(Integer, ForeignKey('sms_templates.id', ondelete='SET NULL'), nullable=True)
    custom_message = Column(Text, nullable=True)
    enabled = Column(Boolean, default=True, nullable=False)
    send_to_all_guests = Column(Boolean, default=False, nullable=False)
    min_stay_nights = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    template = relationship("SMSTemplate", back_populates="scheduled_messages")
    send_log = relationship("ScheduledSMSLog", back_populates="scheduled_sms", cascade="all, delete-orphan")

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'name': self.name,
            'trigger_type': self.trigger_type,
            'trigger_offset_hours': self.trigger_offset_hours,
            'trigger_time': self.trigger_time.isoformat() if self.trigger_time else None,
            'template_id': self.template_id,
            'custom_message': self.custom_message,
            'enabled': self.enabled,
            'send_to_all_guests': self.send_to_all_guests,
            'min_stay_nights': self.min_stay_nights,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class ScheduledSMSLog(Base):
    """Log of sent scheduled SMS messages (prevents duplicates)."""
    __tablename__ = 'scheduled_sms_log'

    id = Column(Integer, primary_key=True, autoincrement=True)
    scheduled_sms_id = Column(Integer, ForeignKey('scheduled_sms.id', ondelete='CASCADE'), nullable=False)
    calendar_event_id = Column(Integer, ForeignKey('calendar_events.id', ondelete='CASCADE'), nullable=False)
    sms_history_id = Column(Integer, ForeignKey('sms_history.id', ondelete='SET NULL'), nullable=True)
    sent_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    status = Column(String(20), default='sent', nullable=False)

    # Relationships
    scheduled_sms = relationship("ScheduledSMS", back_populates="send_log")
    calendar_event = relationship("CalendarEvent")
    sms_history = relationship("SMSHistory")

    __table_args__ = (
        UniqueConstraint('scheduled_sms_id', 'calendar_event_id', name='uq_scheduled_log_combo'),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'scheduled_sms_id': self.scheduled_sms_id,
            'calendar_event_id': self.calendar_event_id,
            'sms_history_id': self.sms_history_id,
            'sent_at': self.sent_at.isoformat() if self.sent_at else None,
            'status': self.status,
        }


class PendingSMS(Base):
    """Queue for delayed/scheduled SMS sends."""
    __tablename__ = 'pending_sms'

    id = Column(Integer, primary_key=True, autoincrement=True)
    calendar_event_id = Column(Integer, ForeignKey('calendar_events.id', ondelete='CASCADE'), nullable=True)
    phone_number = Column(String(50), nullable=False)
    content = Column(Text, nullable=False)
    content_type = Column(String(50), nullable=True)
    scheduled_for = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(20), default='pending', nullable=False)  # pending, sent, cancelled, failed
    original_query = Column(Text, nullable=True)
    session_id = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    sms_history_id = Column(Integer, ForeignKey('sms_history.id', ondelete='SET NULL'), nullable=True)

    # Relationships
    calendar_event = relationship("CalendarEvent")
    sms_history = relationship("SMSHistory")

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'calendar_event_id': self.calendar_event_id,
            'phone_number': self.phone_number,
            'content': self.content,
            'content_type': self.content_type,
            'scheduled_for': self.scheduled_for.isoformat() if self.scheduled_for else None,
            'status': self.status,
            'original_query': self.original_query,
            'session_id': self.session_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'sent_at': self.sent_at.isoformat() if self.sent_at else None,
            'sms_history_id': self.sms_history_id,
        }


class SMSIncoming(Base):
    """Log of incoming SMS for bidirectional conversations."""
    __tablename__ = 'sms_incoming'

    id = Column(Integer, primary_key=True, autoincrement=True)
    phone_number = Column(String(50), nullable=False)
    message = Column(Text, nullable=False)
    twilio_sid = Column(String(100), nullable=True)
    calendar_event_id = Column(Integer, ForeignKey('calendar_events.id', ondelete='SET NULL'), nullable=True)
    matched_guest = Column(Boolean, default=False, nullable=False)
    response_sent = Column(Boolean, default=False, nullable=False)
    response_content = Column(Text, nullable=True)
    received_at = Column(DateTime(timezone=True), nullable=False)
    processed_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    calendar_event = relationship("CalendarEvent")

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'phone_number': self.phone_number,
            'message': self.message,
            'twilio_sid': self.twilio_sid,
            'calendar_event_id': self.calendar_event_id,
            'matched_guest': self.matched_guest,
            'response_sent': self.response_sent,
            'response_content': self.response_content,
            'received_at': self.received_at.isoformat() if self.received_at else None,
            'processed_at': self.processed_at.isoformat() if self.processed_at else None,
        }


# ============================================================================
# Room Group Models - For logical room grouping and aliases
# ============================================================================

class RoomGroup(Base):
    """
    Logical grouping of rooms (e.g., "first floor", "downstairs", "bedrooms").

    Allows users to control multiple rooms with a single command like
    "turn on the lights on the first floor" or "set downstairs to blue".
    """
    __tablename__ = 'room_groups'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False, index=True)  # Canonical name: "first_floor"
    display_name = Column(String(200), nullable=False)  # User-friendly: "First Floor"
    description = Column(Text, nullable=True)  # Optional description
    enabled = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    aliases = relationship("RoomGroupAlias", back_populates="room_group", cascade="all, delete-orphan")
    members = relationship("RoomGroupMember", back_populates="room_group", cascade="all, delete-orphan")

    __table_args__ = (
        Index('idx_room_groups_enabled', 'enabled'),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'name': self.name,
            'display_name': self.display_name,
            'description': self.description,
            'enabled': self.enabled,
            'aliases': [a.alias for a in self.aliases] if self.aliases else [],
            'members': [m.to_dict() for m in self.members] if self.members else [],
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class RoomGroupAlias(Base):
    """
    Aliases for room groups to support natural language variations.

    Examples:
    - "first floor" aliases: "1st floor", "main floor", "ground floor", "downstairs"
    - "second floor" aliases: "2nd floor", "upstairs"
    """
    __tablename__ = 'room_group_aliases'

    id = Column(Integer, primary_key=True, autoincrement=True)
    room_group_id = Column(Integer, ForeignKey('room_groups.id', ondelete='CASCADE'), nullable=False)
    alias = Column(String(200), nullable=False, index=True)  # The alternate name
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    room_group = relationship("RoomGroup", back_populates="aliases")

    __table_args__ = (
        UniqueConstraint('alias', name='uq_room_group_alias'),
        Index('idx_room_group_aliases_alias_lower', text('lower(alias)')),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'room_group_id': self.room_group_id,
            'alias': self.alias,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class RoomGroupMember(Base):
    """
    Individual rooms that belong to a room group.

    Links room groups to actual room names that the orchestrator understands.
    Can optionally include HA entity patterns for direct entity matching.
    """
    __tablename__ = 'room_group_members'

    id = Column(Integer, primary_key=True, autoincrement=True)
    room_group_id = Column(Integer, ForeignKey('room_groups.id', ondelete='CASCADE'), nullable=False)
    room_name = Column(String(100), nullable=False)  # Room identifier: "living_room", "kitchen"
    display_name = Column(String(200), nullable=True)  # Optional friendly name: "Living Room"
    ha_entity_pattern = Column(String(200), nullable=True)  # Optional HA pattern: "light.living*"
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    room_group = relationship("RoomGroup", back_populates="members")

    __table_args__ = (
        UniqueConstraint('room_group_id', 'room_name', name='uq_room_group_member'),
        Index('idx_room_group_members_room_name', 'room_name'),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'room_group_id': self.room_group_id,
            'room_name': self.room_name,
            'display_name': self.display_name,
            'ha_entity_pattern': self.ha_entity_pattern,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class Guest(Base):
    """
    Guest associated with a calendar event (reservation).

    Supports multiple guests per reservation. Primary guest is imported from
    iCal data, additional guests can be added via web app or voice interaction.

    Future: voice_profile_id for voice fingerprinting identification.
    """
    __tablename__ = 'guests'

    id = Column(Integer, primary_key=True)
    calendar_event_id = Column(Integer, ForeignKey('calendar_events.id', ondelete='CASCADE'))
    name = Column(String(100), nullable=False)
    email = Column(String(100))
    phone = Column(String(20))
    is_primary = Column(Boolean, default=False, nullable=False)
    voice_profile_id = Column(String(255))  # Future: voice fingerprinting
    is_test = Column(Boolean, default=False, nullable=False)  # Test mode data
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    calendar_event = relationship('CalendarEvent', backref='guests')
    sessions = relationship('UserSession', back_populates='guest', cascade='all, delete-orphan')

    __table_args__ = (
        Index('idx_guests_calendar_event', 'calendar_event_id'),
        Index('idx_guests_voice_profile', 'voice_profile_id'),
        Index('idx_guests_is_primary', 'is_primary'),
        Index('idx_guests_is_test', 'is_test'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert guest to dictionary for API responses."""
        return {
            'id': self.id,
            'calendar_event_id': self.calendar_event_id,
            'name': self.name,
            'email': self.email,
            'phone': self.phone,
            'is_primary': self.is_primary,
            'voice_profile_id': self.voice_profile_id,
            'is_test': self.is_test,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class UserSession(Base):
    """
    User session mapping device fingerprint to guest.

    Enables device-based identification across web app and API interactions.
    Supports future voice session tracking via device_type='voice'.
    """
    __tablename__ = 'user_sessions'

    id = Column(Integer, primary_key=True)
    session_id = Column(String(255), unique=True, nullable=False)
    guest_id = Column(Integer, ForeignKey('guests.id', ondelete='CASCADE'))
    device_id = Column(String(255), nullable=False)
    device_type = Column(String(50), default='web', nullable=False)  # 'web', 'mobile', 'voice'
    room = Column(String(50))
    last_seen = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    preferences = Column(JSONB, default={})
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    guest = relationship('Guest', back_populates='sessions')

    __table_args__ = (
        UniqueConstraint('session_id', name='uq_user_session_id'),
        Index('idx_user_sessions_device', 'device_id'),
        Index('idx_user_sessions_guest', 'guest_id'),
        Index('idx_user_sessions_last_seen', 'last_seen'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert session to dictionary for API responses."""
        return {
            'id': self.id,
            'session_id': self.session_id,
            'guest_id': self.guest_id,
            'guest_name': self.guest.name if self.guest else None,
            'device_id': self.device_id,
            'device_type': self.device_type,
            'room': self.room,
            'last_seen': self.last_seen.isoformat() if self.last_seen else None,
            'preferences': self.preferences or {},
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# Export all models for Alembic
__all__ = [
    'Base', 'User', 'Policy', 'PolicyVersion', 'Secret', 'Device', 'AuditLog',
    'ServerConfig', 'ServiceRegistry', 'RAGConnector', 'RAGStats', 'VoiceTest', 'VoiceTestFeedback',
    'IntentCategory', 'HallucinationCheck', 'CrossValidationModel', 'MultiIntentConfig',
    'IntentChainRule', 'ValidationTestScenario', 'ConfidenceScoreRule', 'ResponseEnhancementRule',
    'ConversationSettings', 'ClarificationSettings', 'ClarificationType',
    'SportsTeamDisambiguation', 'DeviceDisambiguationRule', 'ConversationAnalytics',
    'LLMBackend', 'LLMPerformanceMetric', 'Feature',
    'IntentPattern', 'IntentRouting', 'ProviderRouting',
    'GuestModeConfig', 'CalendarEvent', 'ModeOverride',
    'ToolRegistry', 'ToolCallingSetting', 'ToolCallingTrigger', 'ToolUsageMetric',
    'BaseKnowledge', 'ComponentModelAssignment', 'AthenaService', 'SystemSetting',
    'GatewayConfig',
    # SMS Models
    'SMSSettings', 'GuestSMSPreference', 'SMSHistory', 'SMSCostTracking',
    # SMS Enhanced Features (Phase 2)
    'TipPrompt', 'TipPromptHistory', 'SMSTemplate', 'ScheduledSMS',
    'ScheduledSMSLog', 'PendingSMS', 'SMSIncoming',
    # Room Groups
    'RoomGroup', 'RoomGroupAlias', 'RoomGroupMember',
    # Multi-Guest Support
    'Guest', 'UserSession',
    # Hierarchical Memory System
    'GuestSession', 'Memory', 'MemoryConfig',
    # LiveKit WebRTC
    'LiveKitConfig',
]


# =============================================================================
# Hierarchical Memory System Models
# =============================================================================

class GuestSession(Base):
    """
    Guest session tracking for hierarchical memory system.

    Links to CalendarEvent (Lodgify bookings) to define memory boundaries.
    Memories created during a guest stay are scoped to this session.
    """
    __tablename__ = 'guest_sessions'

    id = Column(Integer, primary_key=True)

    # Link to calendar event (Lodgify booking)
    calendar_event_id = Column(Integer, ForeignKey('calendar_events.id', ondelete='CASCADE'), nullable=True)
    lodgify_booking_id = Column(String(100), unique=True, nullable=True)

    # Guest info (denormalized for display)
    guest_name = Column(String(255))
    guest_email = Column(String(255))

    # Session boundaries
    check_in_date = Column(Date, nullable=False)
    check_out_date = Column(Date, nullable=False)
    actual_check_in = Column(DateTime(timezone=True))
    actual_check_out = Column(DateTime(timezone=True))

    # Status: 'upcoming', 'active', 'completed', 'cancelled'
    status = Column(String(20), nullable=False, default='upcoming')

    # Metadata
    notes = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    calendar_event = relationship('CalendarEvent', backref='guest_session')
    memories = relationship('Memory', back_populates='guest_session', cascade='all, delete-orphan')

    __table_args__ = (
        Index('idx_guest_sessions_status', 'status'),
        Index('idx_guest_sessions_dates', 'check_in_date', 'check_out_date'),
        Index('idx_guest_sessions_lodgify', 'lodgify_booking_id'),
        Index('idx_guest_sessions_calendar_event', 'calendar_event_id'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'calendar_event_id': self.calendar_event_id,
            'lodgify_booking_id': self.lodgify_booking_id,
            'guest_name': self.guest_name,
            'guest_email': self.guest_email,
            'check_in_date': self.check_in_date.isoformat() if self.check_in_date else None,
            'check_out_date': self.check_out_date.isoformat() if self.check_out_date else None,
            'actual_check_in': self.actual_check_in.isoformat() if self.actual_check_in else None,
            'actual_check_out': self.actual_check_out.isoformat() if self.actual_check_out else None,
            'status': self.status,
            'notes': self.notes,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class Memory(Base):
    """
    Hierarchical memory storage with Qdrant vector integration.

    Supports three scopes:
    - 'global': Facts available to everyone (promoted from owner/guest)
    - 'owner': Jay's persistent memories (never expires)
    - 'guest': Scoped to a GuestSession (expires after retention period)
    """
    __tablename__ = 'memories'

    id = Column(Integer, primary_key=True)

    # Content
    content = Column(Text, nullable=False)
    summary = Column(String(255))  # Short description for UI

    # Scoping: 'global', 'owner', 'guest'
    scope = Column(String(20), nullable=False, index=True)
    guest_session_id = Column(Integer, ForeignKey('guest_sessions.id', ondelete='CASCADE'), nullable=True)

    # Qdrant vector reference
    vector_id = Column(String(36), nullable=False, unique=True, index=True)  # UUID as string
    collection = Column(String(100), nullable=False, default='athena_memories')

    # Metadata
    category = Column(String(100), index=True)  # preference, fact, context, conversation
    importance = Column(Float, nullable=False, default=0.5)
    access_count = Column(Integer, nullable=False, default=0)
    last_accessed_at = Column(DateTime(timezone=True))

    # Source tracking
    source_type = Column(String(50))  # conversation, manual, promotion
    source_query = Column(Text)  # Original query that created this memory
    promoted_from_id = Column(Integer, ForeignKey('memories.id', ondelete='SET NULL'), nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    expires_at = Column(DateTime(timezone=True))  # NULL for global/owner, set for guest

    # Soft delete
    is_deleted = Column(Boolean, nullable=False, default=False)
    deleted_at = Column(DateTime(timezone=True))

    # Relationships
    guest_session = relationship('GuestSession', back_populates='memories')
    promoted_from = relationship('Memory', remote_side=[id], backref='promoted_to')

    __table_args__ = (
        Index('idx_memories_scope', 'scope'),
        Index('idx_memories_guest_session', 'guest_session_id'),
        Index('idx_memories_category', 'category'),
        Index('idx_memories_expires_at', 'expires_at'),
        Index('idx_memories_is_deleted', 'is_deleted'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'content': self.content,
            'summary': self.summary,
            'scope': self.scope,
            'guest_session_id': self.guest_session_id,
            'vector_id': self.vector_id,
            'collection': self.collection,
            'category': self.category,
            'importance': self.importance,
            'access_count': self.access_count,
            'last_accessed_at': self.last_accessed_at.isoformat() if self.last_accessed_at else None,
            'source_type': self.source_type,
            'source_query': self.source_query,
            'promoted_from_id': self.promoted_from_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'is_deleted': self.is_deleted,
        }


class MemoryConfig(Base):
    """
    Configuration settings for the hierarchical memory system.

    Stores key-value pairs for retention, limits, auto-creation settings, etc.
    """
    __tablename__ = 'memory_config'

    id = Column(Integer, primary_key=True)
    key = Column(String(100), unique=True, nullable=False, index=True)
    value = Column(JSONB, nullable=False)
    description = Column(Text)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index('idx_memory_config_key', 'key'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'key': self.key,
            'value': self.value,
            'description': self.description,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


# =============================================================================
# Directions Settings
# =============================================================================


class DirectionsSettings(Base):
    """
    Configurable settings for the Directions RAG service.

    Stores default values and behavior configuration for route planning.
    Settings are fetched by the RAG service at startup and cached.
    """
    __tablename__ = 'directions_settings'

    id = Column(Integer, primary_key=True)
    setting_key = Column(String(100), nullable=False, unique=True, index=True)
    setting_value = Column(String(500), nullable=False)
    setting_type = Column(String(50), nullable=False)  # string, integer, boolean, json
    display_name = Column(String(200), nullable=False)
    description = Column(Text)
    category = Column(String(50), nullable=False, default='general')
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index('idx_directions_settings_key', 'setting_key'),
        Index('idx_directions_settings_category', 'category'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary with typed value."""
        import json as json_module
        value = self.setting_value
        if self.setting_type == 'boolean':
            value = self.setting_value.lower() == 'true'
        elif self.setting_type == 'integer':
            value = int(self.setting_value)
        elif self.setting_type == 'json':
            value = json_module.loads(self.setting_value)

        return {
            'id': self.id,
            'setting_key': self.setting_key,
            'setting_value': value,
            'raw_value': self.setting_value,
            'setting_type': self.setting_type,
            'display_name': self.display_name,
            'description': self.description,
            'category': self.category,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


# =============================================================================
# Intent Discovery - Emerging Intents
# =============================================================================


class EmergingIntent(Base):
    """
    Tracks novel/unknown intents discovered during classification.

    When the intent classifier has low confidence or classifies as "unknown",
    an LLM generates a canonical intent name. Similar intents are clustered
    using embedding similarity to avoid duplicates.

    Admin can review, promote to known intents, or reject.
    """
    __tablename__ = 'emerging_intents'

    id = Column(Integer, primary_key=True)

    # Intent identification
    canonical_name = Column(String(100), nullable=False, unique=True, index=True)
    display_name = Column(String(200))
    description = Column(Text)

    # Semantic clustering - embedding stored as JSON array
    # 384 dimensions for all-MiniLM-L6-v2 model
    embedding = Column(JSON)

    # Metrics
    occurrence_count = Column(Integer, nullable=False, default=1)
    first_seen = Column(DateTime(timezone=True), server_default=func.now())
    last_seen = Column(DateTime(timezone=True), server_default=func.now())

    # Sample data for analysis
    sample_queries = Column(JSON, default=list)  # Up to 10 example queries

    # LLM suggestions
    suggested_category = Column(String(50))  # utility, commerce, health, etc.
    suggested_api_sources = Column(JSON)  # Potential APIs to power this

    # Admin workflow
    status = Column(String(20), nullable=False, default='discovered')
    reviewed_at = Column(DateTime(timezone=True))
    reviewed_by = Column(Integer, ForeignKey('users.id'))
    promoted_to_intent = Column(String(50))  # If promoted, which IntentCategory
    rejection_reason = Column(Text)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    reviewer = relationship('User', foreign_keys=[reviewed_by])

    __table_args__ = (
        Index('idx_emerging_intents_canonical_name', 'canonical_name'),
        Index('idx_emerging_intents_status', 'status'),
        Index('idx_emerging_intents_count', 'occurrence_count'),
        Index('idx_emerging_intents_category', 'suggested_category'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'canonical_name': self.canonical_name,
            'display_name': self.display_name,
            'description': self.description,
            'occurrence_count': self.occurrence_count,
            'first_seen': self.first_seen.isoformat() if self.first_seen else None,
            'last_seen': self.last_seen.isoformat() if self.last_seen else None,
            'sample_queries': self.sample_queries or [],
            'suggested_category': self.suggested_category,
            'suggested_api_sources': self.suggested_api_sources,
            'status': self.status,
            'reviewed_at': self.reviewed_at.isoformat() if self.reviewed_at else None,
            'reviewed_by': self.reviewed_by,
            'promoted_to_intent': self.promoted_to_intent,
            'rejection_reason': self.rejection_reason,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class IntentMetric(Base):
    """
    Records all intent classifications for analytics.

    Tracks both known and novel intent classifications to provide insights
    into user behavior and system performance.
    """
    __tablename__ = 'intent_metrics'

    id = Column(Integer, primary_key=True)

    # Classification result
    intent = Column(String(50), nullable=False, index=True)
    confidence = Column(Float, nullable=False)
    complexity = Column(String(20))  # simple, complex, super_complex

    # Novel intent tracking
    is_novel = Column(Boolean, default=False, index=True)
    emerging_intent_id = Column(Integer, ForeignKey('emerging_intents.id'), index=True)

    # Query info
    raw_query = Column(Text)
    query_hash = Column(String(64))  # MD5 hash for deduplication

    # Context
    session_id = Column(String(100), index=True)
    mode = Column(String(20))  # owner, guest
    room = Column(String(50))

    # Request metadata
    request_id = Column(String(50))
    processing_time_ms = Column(Integer)

    # Timestamp
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    # Relationships
    emerging_intent = relationship('EmergingIntent', foreign_keys=[emerging_intent_id])

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'intent': self.intent,
            'confidence': self.confidence,
            'complexity': self.complexity,
            'is_novel': self.is_novel,
            'emerging_intent_id': self.emerging_intent_id,
            'raw_query': self.raw_query,
            'session_id': self.session_id,
            'mode': self.mode,
            'room': self.room,
            'request_id': self.request_id,
            'processing_time_ms': self.processing_time_ms,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# =============================================================================
# Music Configuration
# =============================================================================


class MusicConfig(Base):
    """
    Global music playback configuration.

    Stores Music Assistant connection settings, Spotify account pool,
    default playback preferences, and genre-to-artist mappings.
    """
    __tablename__ = 'music_config'

    id = Column(Integer, primary_key=True)

    # Music Assistant Connection
    music_assistant_url = Column(String(255))  # e.g., "http://homeassistant.local:8095"
    music_assistant_enabled = Column(Boolean, default=False)

    # Spotify Account Pool (for independent multi-room playback)
    # Format: [{"id": "account1", "name": "Primary"}, ...]
    spotify_accounts = Column(JSONB, default=list)

    # Default Playback Settings
    default_volume = Column(Float, default=0.5)  # 0.0 - 1.0
    default_radio_mode = Column(Boolean, default=True)
    default_provider = Column(String(50), default="music_assistant")

    # Genre Mappings (multiple seed artists per genre)
    # Format: {"jazz": ["Miles Davis", "John Coltrane", ...], ...}
    genre_to_artists = Column(JSONB, default=dict)

    # Seed Artist Selection Mode: random, first, rotate
    genre_seed_selection_mode = Column(String(20), default="random")

    # Health Monitoring
    stream_health_check_enabled = Column(Boolean, default=True)
    stream_frozen_timeout_seconds = Column(Integer, default=30)
    auto_restart_frozen_streams = Column(Boolean, default=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'music_assistant_url': self.music_assistant_url,
            'music_assistant_enabled': self.music_assistant_enabled,
            'spotify_accounts': self.spotify_accounts or [],
            'default_volume': self.default_volume,
            'default_radio_mode': self.default_radio_mode,
            'default_provider': self.default_provider,
            'genre_to_artists': self.genre_to_artists or {},
            'genre_seed_selection_mode': self.genre_seed_selection_mode,
            'stream_health_check_enabled': self.stream_health_check_enabled,
            'stream_frozen_timeout_seconds': self.stream_frozen_timeout_seconds,
            'auto_restart_frozen_streams': self.auto_restart_frozen_streams,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class RoomAudioConfig(Base):
    """
    Per-room audio output configuration.

    Configures speaker setup for each room: single speaker, stereo pair,
    or speaker group. Replaces hardcoded ROOM_TO_MUSIC_PLAYER mapping.
    """
    __tablename__ = 'room_audio_configs'

    id = Column(Integer, primary_key=True)

    # Room identification
    room_name = Column(String(100), unique=True, nullable=False, index=True)
    display_name = Column(String(100))

    # Speaker configuration
    speaker_type = Column(String(20), default="single")  # single, stereo_pair, group
    primary_entity_id = Column(String(255), nullable=False)  # media_player.office
    secondary_entity_id = Column(String(255))  # For stereo pairs (left/right)
    group_entity_ids = Column(JSONB, default=list)  # For groups: ["entity1", "entity2"]

    # Preferences
    default_volume = Column(Float, default=0.5)  # 0.0 - 1.0
    preferred_provider = Column(String(50), default="music_assistant")
    use_radio_mode = Column(Boolean, default=True)

    # Status
    enabled = Column(Boolean, default=True)
    last_tested_at = Column(DateTime(timezone=True))
    last_test_result = Column(String(50))  # success, failed, timeout

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index('idx_room_audio_configs_room_name', 'room_name'),
        Index('idx_room_audio_configs_enabled', 'enabled'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'room_name': self.room_name,
            'display_name': self.display_name,
            'speaker_type': self.speaker_type,
            'primary_entity_id': self.primary_entity_id,
            'secondary_entity_id': self.secondary_entity_id,
            'group_entity_ids': self.group_entity_ids or [],
            'default_volume': self.default_volume,
            'preferred_provider': self.preferred_provider,
            'use_radio_mode': self.use_radio_mode,
            'enabled': self.enabled,
            'last_tested_at': self.last_tested_at.isoformat() if self.last_tested_at else None,
            'last_test_result': self.last_test_result,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class RoomTVConfig(Base):
    """
    Per-room Apple TV configuration.

    Maps rooms to Apple TV media player and remote entities for voice control.
    """
    __tablename__ = 'room_tv_configs'

    id = Column(Integer, primary_key=True)

    # Room identification
    room_name = Column(String(100), unique=True, nullable=False, index=True)
    display_name = Column(String(100), nullable=False)

    # Apple TV entities
    media_player_entity_id = Column(String(255), nullable=False)  # media_player.master_bedroom_tv
    remote_entity_id = Column(String(255), nullable=False)  # remote.master_bedroom_tv

    # Status
    enabled = Column(Boolean, default=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index('idx_room_tv_configs_room_name', 'room_name'),
        Index('idx_room_tv_configs_enabled', 'enabled'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'room_name': self.room_name,
            'display_name': self.display_name,
            'media_player_entity_id': self.media_player_entity_id,
            'remote_entity_id': self.remote_entity_id,
            'enabled': self.enabled,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class TVAppConfig(Base):
    """
    Apple TV app configuration.

    Stores per-app settings including profile screen handling, guest access,
    and deep link schemes. Seeded with known apps from testing.
    """
    __tablename__ = 'tv_app_configs'

    id = Column(Integer, primary_key=True)

    # App identification
    app_name = Column(String(100), unique=True, nullable=False)  # Exact name from source_list
    display_name = Column(String(100), nullable=False)  # User-friendly name
    icon_url = Column(String(500))  # URL to app icon

    # Profile screen handling
    has_profile_screen = Column(Boolean, default=False)  # Netflix, YouTube, Disney+, etc.
    profile_select_delay_ms = Column(Integer, default=1500)  # Delay before sending select

    # Access control
    guest_allowed = Column(Boolean, default=True)  # Show to guests

    # Deep linking
    deep_link_scheme = Column(String(100))  # youtube://, hulu://, etc.

    # Status
    enabled = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)  # Display order in UI

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index('idx_tv_app_configs_enabled', 'enabled'),
        Index('idx_tv_app_configs_guest', 'guest_allowed'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'app_name': self.app_name,
            'display_name': self.display_name,
            'icon_url': self.icon_url,
            'has_profile_screen': self.has_profile_screen,
            'profile_select_delay_ms': self.profile_select_delay_ms,
            'guest_allowed': self.guest_allowed,
            'deep_link_scheme': self.deep_link_scheme,
            'enabled': self.enabled,
            'sort_order': self.sort_order,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class TVFeatureFlag(Base):
    """
    Feature flags for TV control.

    Controls optional features like multi-TV commands, auto profile select, etc.
    """
    __tablename__ = 'tv_feature_flags'

    id = Column(Integer, primary_key=True)

    feature_name = Column(String(100), unique=True, nullable=False)
    enabled = Column(Boolean, default=True)
    description = Column(Text)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'feature_name': self.feature_name,
            'enabled': self.enabled,
            'description': self.description,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# =============================================================================
# Voice Services Configuration (STT/TTS)
# =============================================================================

class STTModel(Base):
    """
    Speech-to-Text model configuration.

    Stores available Whisper models with their specifications.
    Only one model can be active at a time.
    """
    __tablename__ = 'stt_models'

    id = Column(Integer, primary_key=True)

    # Model identification
    name = Column(String(50), unique=True, nullable=False)  # tiny-int8, base.en, etc.
    display_name = Column(String(100), nullable=False)  # User-friendly name
    engine = Column(String(50), nullable=False, default='faster-whisper')
    model_name = Column(String(100), nullable=False)  # Model name for container

    # Specifications
    description = Column(Text)
    size_mb = Column(Integer)

    # Status
    is_active = Column(Boolean, default=False)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index('idx_stt_models_is_active', 'is_active'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'name': self.name,
            'display_name': self.display_name,
            'engine': self.engine,
            'model_name': self.model_name,
            'description': self.description,
            'size_mb': self.size_mb,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class TTSVoice(Base):
    """
    Text-to-Speech voice configuration.

    Stores available Piper voices with their specifications.
    Only one voice can be active at a time.
    """
    __tablename__ = 'tts_voices'

    id = Column(Integer, primary_key=True)

    # Voice identification
    name = Column(String(50), unique=True, nullable=False)  # lessac-medium, ryan-high, etc.
    display_name = Column(String(100), nullable=False)  # User-friendly name
    engine = Column(String(50), nullable=False, default='piper')
    voice_id = Column(String(100), nullable=False)  # en_US-lessac-medium

    # Specifications
    language = Column(String(10), default='en')
    quality = Column(String(20), default='medium')  # low, medium, high
    description = Column(Text)
    sample_url = Column(String(500))  # URL to voice sample audio

    # Status
    is_active = Column(Boolean, default=False)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index('idx_tts_voices_is_active', 'is_active'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'name': self.name,
            'display_name': self.display_name,
            'engine': self.engine,
            'voice_id': self.voice_id,
            'language': self.language,
            'quality': self.quality,
            'description': self.description,
            'sample_url': self.sample_url,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class VoiceServiceConfig(Base):
    """
    Voice service host/port configuration.

    Stores connection details for STT and TTS services on Mac mini.
    """
    __tablename__ = 'voice_service_config'

    id = Column(Integer, primary_key=True)

    # Service type
    service_type = Column(String(10), unique=True, nullable=False)  # 'stt' or 'tts'

    # Connection details
    host = Column(String(100), nullable=False)  # e.g., localhost or server hostname
    wyoming_port = Column(Integer, nullable=False)  # Wyoming protocol port
    rest_port = Column(Integer)  # REST API port (optional)

    # Status
    enabled = Column(Boolean, default=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'service_type': self.service_type,
            'host': self.host,
            'wyoming_port': self.wyoming_port,
            'rest_port': self.rest_port,
            'enabled': self.enabled,
            'wyoming_url': f"tcp://{self.host}:{self.wyoming_port}",
            'rest_url': f"http://{self.host}:{self.rest_port}" if self.rest_port else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


# =============================================================================
# Hybrid Tool Registry & Admin Jarvis (Phase 1 - Migration 033)
# =============================================================================

class VoiceInterface(Base):
    """
    Voice interface configuration for per-interface STT/TTS routing.

    Defines how each interface (web_jarvis, home_assistant, admin_jarvis)
    handles speech-to-text and text-to-speech, including wake word settings.
    """
    __tablename__ = 'voice_interfaces'

    id = Column(Integer, primary_key=True)
    interface_name = Column(String(100), unique=True, nullable=False)  # 'web_jarvis', 'home_assistant', 'admin_jarvis'
    display_name = Column(String(200))
    description = Column(Text)
    enabled = Column(Boolean, default=True, nullable=False)

    # STT Configuration
    stt_engine = Column(String(50), nullable=False, default='faster-whisper')
    stt_config = Column(JSONB, default=dict)  # Engine-specific config (model, language, etc.)

    # TTS Configuration
    tts_engine = Column(String(50), nullable=False, default='piper')
    tts_config = Column(JSONB, default=dict)  # Engine-specific config (voice, speed, etc.)

    # Behavior Configuration
    wake_word_enabled = Column(Boolean, default=False, nullable=False)
    wake_word = Column(String(100))  # 'jarvis', 'athena', 'hey cal', custom
    continuous_conversation = Column(Boolean, default=True, nullable=False)  # Keep listening after response
    debug_mode = Column(Boolean, default=False, nullable=False)  # Show extra info in responses

    # Rate Limiting
    max_requests_per_minute = Column(Integer, default=30, nullable=False)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index('idx_voice_interfaces_enabled', 'enabled'),
        Index('idx_voice_interfaces_name', 'interface_name'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'interface_name': self.interface_name,
            'display_name': self.display_name,
            'description': self.description,
            'enabled': self.enabled,
            'stt_engine': self.stt_engine,
            'stt_config': self.stt_config or {},
            'tts_engine': self.tts_engine,
            'tts_config': self.tts_config or {},
            'wake_word_enabled': self.wake_word_enabled,
            'wake_word': self.wake_word,
            'continuous_conversation': self.continuous_conversation,
            'debug_mode': self.debug_mode,
            'max_requests_per_minute': self.max_requests_per_minute,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class STTEngine(Base):
    """
    Speech-to-Text engine registry.

    Available STT engine types with their configurations.
    Different from STTModel which stores specific models within an engine.
    """
    __tablename__ = 'stt_engines'

    id = Column(Integer, primary_key=True)
    engine_name = Column(String(50), unique=True, nullable=False)
    display_name = Column(String(100))
    description = Column(Text)
    endpoint_url = Column(String(500))
    enabled = Column(Boolean, default=True, nullable=False)
    requires_gpu = Column(Boolean, default=False, nullable=False)
    is_cloud = Column(Boolean, default=False, nullable=False)  # True for OpenAI, etc.
    default_config = Column(JSONB, default=dict)
    supported_languages = Column(JSONB, default=list)  # e.g., ["en", "es", "fr"]

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index('idx_stt_engines_enabled', 'enabled'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'engine_name': self.engine_name,
            'display_name': self.display_name,
            'description': self.description,
            'endpoint_url': self.endpoint_url,
            'enabled': self.enabled,
            'requires_gpu': self.requires_gpu,
            'is_cloud': self.is_cloud,
            'default_config': self.default_config or {},
            'supported_languages': self.supported_languages or ['en'],
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class TTSEngine(Base):
    """
    Text-to-Speech engine registry.

    Available TTS engine types with their configurations.
    Different from TTSVoice which stores specific voices within an engine.
    """
    __tablename__ = 'tts_engines'

    id = Column(Integer, primary_key=True)
    engine_name = Column(String(50), unique=True, nullable=False)
    display_name = Column(String(100))
    description = Column(Text)
    endpoint_url = Column(String(500))
    enabled = Column(Boolean, default=True, nullable=False)
    requires_gpu = Column(Boolean, default=False, nullable=False)
    is_cloud = Column(Boolean, default=False, nullable=False)
    default_config = Column(JSONB, default=dict)
    available_voices = Column(JSONB, default=list)  # List of voice IDs

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index('idx_tts_engines_enabled', 'enabled'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'engine_name': self.engine_name,
            'display_name': self.display_name,
            'description': self.description,
            'endpoint_url': self.endpoint_url,
            'enabled': self.enabled,
            'requires_gpu': self.requires_gpu,
            'is_cloud': self.is_cloud,
            'default_config': self.default_config or {},
            'available_voices': self.available_voices or [],
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class MCPSecurity(Base):
    """
    MCP Security Configuration.

    Security settings for MCP tool discovery including domain restrictions,
    execution limits, and approval workflow configuration.
    """
    __tablename__ = 'mcp_security'

    id = Column(Integer, primary_key=True)

    # Domain restrictions
    allowed_domains = Column(JSONB, default=list)  # e.g., ["localhost", "localhost"]
    blocked_domains = Column(JSONB, default=list)

    # Execution limits
    max_execution_time_ms = Column(Integer, default=30000, nullable=False)  # 30 second timeout
    max_concurrent_tools = Column(Integer, default=5, nullable=False)

    # Approval workflow
    require_owner_approval = Column(Boolean, default=True, nullable=False)
    auto_approve_patterns = Column(JSONB, default=list)  # Tool name patterns for auto-approval

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'allowed_domains': self.allowed_domains or [],
            'blocked_domains': self.blocked_domains or [],
            'max_execution_time_ms': self.max_execution_time_ms,
            'max_concurrent_tools': self.max_concurrent_tools,
            'require_owner_approval': self.require_owner_approval,
            'auto_approve_patterns': self.auto_approve_patterns or [],
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class ToolApprovalQueue(Base):
    """
    Tool Approval Queue for MCP-discovered tools.

    Queue for approving discovered MCP tools before they become active.
    Implements owner approval workflow for security.
    """
    __tablename__ = 'tool_approval_queue'

    id = Column(Integer, primary_key=True)
    tool_name = Column(String(200), nullable=False)
    tool_source = Column(String(50), nullable=False)  # 'mcp', 'n8n'
    discovery_url = Column(String(500))
    input_schema = Column(JSONB)
    description = Column(Text)
    status = Column(String(20), default='pending', nullable=False)  # 'pending', 'approved', 'rejected'
    approved_by_id = Column(Integer, ForeignKey('users.id'))
    approved_at = Column(DateTime(timezone=True))
    rejection_reason = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Relationships
    approved_by = relationship('User', foreign_keys=[approved_by_id])

    __table_args__ = (
        Index('idx_tool_approval_status', 'status'),
        Index('idx_tool_approval_source', 'tool_source'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'tool_name': self.tool_name,
            'tool_source': self.tool_source,
            'discovery_url': self.discovery_url,
            'input_schema': self.input_schema,
            'description': self.description,
            'status': self.status,
            'approved_by': self.approved_by.username if self.approved_by else None,
            'approved_at': self.approved_at.isoformat() if self.approved_at else None,
            'rejection_reason': self.rejection_reason,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class PipelineEvent(Base):
    """
    Pipeline Events for Admin Jarvis real-time visualization.

    Stores events during pipeline execution for:
    - Intent classification
    - Tool selection and execution
    - Response generation
    - Performance tracking
    """
    __tablename__ = 'pipeline_events'

    id = Column(Integer, primary_key=True)
    session_id = Column(String(100), nullable=False)
    event_type = Column(String(50), nullable=False)  # 'stt_start', 'intent_classified', 'tool_executing', etc.
    event_data = Column(JSONB, nullable=False)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    interface = Column(String(50))  # 'web_jarvis', 'home_assistant', 'admin_jarvis'
    duration_ms = Column(Integer)  # Time since previous event

    __table_args__ = (
        Index('idx_pipeline_events_session', 'session_id'),
        Index('idx_pipeline_events_timestamp', 'timestamp'),
        Index('idx_pipeline_events_type', 'event_type'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'session_id': self.session_id,
            'event_type': self.event_type,
            'event_data': self.event_data,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'interface': self.interface,
            'duration_ms': self.duration_ms,
        }


# =============================================================================
# Tool Proposals (Self-Building Tools)
# =============================================================================

class ToolProposal(Base):
    """
    Tool Proposals for self-building tools feature.

    Stores LLM-proposed n8n workflow definitions awaiting owner approval.
    """
    __tablename__ = 'tool_proposals'

    id = Column(Integer, primary_key=True)
    proposal_id = Column(String(50), unique=True, nullable=False)  # Short unique ID
    name = Column(String(100), nullable=False)  # Tool name (snake_case)
    description = Column(Text, nullable=False)
    trigger_phrases = Column(JSONB, nullable=False)  # List of trigger phrases
    workflow_definition = Column(JSONB, nullable=False)  # Full n8n workflow JSON

    # Status: 'pending', 'approved', 'rejected', 'deployed', 'failed'
    status = Column(String(20), default='pending', nullable=False)

    # Creation info
    created_by = Column(String(100), default='llm', nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Approval info
    approved_by_id = Column(Integer, ForeignKey('users.id'))
    approved_at = Column(DateTime(timezone=True))
    rejection_reason = Column(Text)

    # Deployment info
    n8n_workflow_id = Column(String(100))  # ID from n8n after deployment
    deployed_at = Column(DateTime(timezone=True))
    error_message = Column(Text)

    # Relationship
    approved_by = relationship('User', foreign_keys=[approved_by_id])

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'proposal_id': self.proposal_id,
            'name': self.name,
            'description': self.description,
            'trigger_phrases': self.trigger_phrases,
            'workflow_definition': self.workflow_definition,
            'status': self.status,
            'created_by': self.created_by,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'approved_by_id': self.approved_by_id,
            'approved_at': self.approved_at.isoformat() if self.approved_at else None,
            'rejection_reason': self.rejection_reason,
            'n8n_workflow_id': self.n8n_workflow_id,
            'deployed_at': self.deployed_at.isoformat() if self.deployed_at else None,
            'error_message': self.error_message,
        }


# =============================================================================
# LiveKit WebRTC Configuration
# =============================================================================

class LiveKitConfig(Base):
    """
    LiveKit WebRTC configuration for browser-based voice streaming.

    Stores connection settings, audio parameters, and wake word configuration
    for the LiveKit integration.
    """
    __tablename__ = 'livekit_config'

    id = Column(Integer, primary_key=True)

    # Connection settings
    livekit_url = Column(String(255), nullable=False)
    api_key_encrypted = Column(Text)  # Encrypted API key
    api_secret_encrypted = Column(Text)  # Encrypted API secret

    # Room settings
    room_empty_timeout = Column(Integer, default=300)  # 5 minutes
    max_participants = Column(Integer, default=2)  # User + Athena

    # Audio settings
    sample_rate = Column(Integer, default=16000)
    channels = Column(Integer, default=1)

    # Wake word settings
    wake_words = Column(JSONB, default=['jarvis', 'athena'])
    wake_word_threshold = Column(Float, default=0.5)

    # VAD settings
    vad_enabled = Column(Boolean, default=True)
    silence_timeout_ms = Column(Integer, default=2000)
    max_query_duration_ms = Column(Integer, default=30000)

    # Feature toggles
    server_side_wake_word = Column(Boolean, default=True)
    client_side_vad = Column(Boolean, default=True)
    interruption_enabled = Column(Boolean, default=True)

    # Status
    enabled = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'livekit_url': self.livekit_url,
            'has_api_key': bool(self.api_key_encrypted),
            'has_api_secret': bool(self.api_secret_encrypted),
            'room_empty_timeout': self.room_empty_timeout,
            'max_participants': self.max_participants,
            'sample_rate': self.sample_rate,
            'channels': self.channels,
            'wake_words': self.wake_words,
            'wake_word_threshold': self.wake_word_threshold,
            'vad_enabled': self.vad_enabled,
            'silence_timeout_ms': self.silence_timeout_ms,
            'max_query_duration_ms': self.max_query_duration_ms,
            'server_side_wake_word': self.server_side_wake_word,
            'client_side_vad': self.client_side_vad,
            'interruption_enabled': self.interruption_enabled,
            'enabled': self.enabled,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


# =============================================================================
# Site Scraper Configuration
# =============================================================================

class SiteScraperConfig(Base):
    """
    Site Scraper service configuration.

    Controls URL access restrictions per user mode and domain whitelists/blacklists.
    """
    __tablename__ = 'site_scraper_config'

    id = Column(Integer, primary_key=True)
    owner_mode_any_url = Column(Boolean, default=True, nullable=False)
    guest_mode_any_url = Column(Boolean, default=False, nullable=False)
    allowed_domains = Column(ARRAY(String), default=list,
                            comment="Whitelist domains for guest mode (empty = allow all)")
    blocked_domains = Column(ARRAY(String), default=list,
                            comment="Blocked domains for all modes")
    max_content_length = Column(Integer, default=50000, nullable=False)
    cache_ttl = Column(Integer, default=1800, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "owner_mode_any_url": self.owner_mode_any_url,
            "guest_mode_any_url": self.guest_mode_any_url,
            "allowed_domains": self.allowed_domains or [],
            "blocked_domains": self.blocked_domains or [],
            "max_content_length": self.max_content_length,
            "cache_ttl": self.cache_ttl
        }


# =============================================================================
# Voice Automations
# =============================================================================

class VoiceAutomation(Base):
    """
    Voice-created automations for scheduled and recurring smart home actions.

    Supports owner and guest-scoped automations with archival for returning guests.
    Integrates with Home Assistant automation API for actual execution.
    """
    __tablename__ = 'voice_automations'

    id = Column(Integer, primary_key=True)

    # Identification
    name = Column(String(255), nullable=False)
    ha_automation_id = Column(String(255), nullable=True, index=True)

    # Ownership
    owner_type = Column(String(20), nullable=False)  # 'owner' or 'guest'
    guest_session_id = Column(String(255), nullable=True, index=True)
    guest_name = Column(String(255), nullable=True, index=True)
    created_by_room = Column(String(100), nullable=True)

    # Automation definition (stored as JSONB for flexibility)
    trigger_config = Column(JSONB, nullable=False)
    conditions_config = Column(JSONB, nullable=True)
    actions_config = Column(JSONB, nullable=False)

    # Scheduling
    is_one_time = Column(Boolean, default=False, nullable=False)
    end_date = Column(Date, nullable=True)

    # Status
    status = Column(String(20), default='active', nullable=False, index=True)
    archived_at = Column(DateTime(timezone=True), nullable=True)
    archive_reason = Column(String(100), nullable=True)

    # Execution tracking
    last_triggered_at = Column(DateTime(timezone=True), nullable=True)
    trigger_count = Column(Integer, default=0, nullable=False)

    # Metadata
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'name': self.name,
            'ha_automation_id': self.ha_automation_id,
            'owner_type': self.owner_type,
            'guest_session_id': self.guest_session_id,
            'guest_name': self.guest_name,
            'created_by_room': self.created_by_room,
            'trigger_config': self.trigger_config,
            'conditions_config': self.conditions_config,
            'actions_config': self.actions_config,
            'is_one_time': self.is_one_time,
            'end_date': self.end_date.isoformat() if self.end_date else None,
            'status': self.status,
            'archived_at': self.archived_at.isoformat() if self.archived_at else None,
            'archive_reason': self.archive_reason,
            'last_triggered_at': self.last_triggered_at.isoformat() if self.last_triggered_at else None,
            'trigger_count': self.trigger_count,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }

    def to_summary(self) -> Dict[str, Any]:
        """Convert to summary for guest restoration prompts."""
        return {
            'id': self.id,
            'name': self.name,
            'description': self._describe_automation(),
            'trigger_count': self.trigger_count,
            'last_used': self.last_triggered_at.isoformat() if self.last_triggered_at else None,
        }

    def _describe_automation(self) -> str:
        """Generate human-readable description of automation."""
        trigger = self.trigger_config or {}
        actions = self.actions_config or []

        trigger_type = trigger.get('type', 'time')
        trigger_desc = ""
        if trigger_type == 'time':
            trigger_desc = f"At {trigger.get('time', 'scheduled time')}"
        elif trigger_type == 'sunset':
            offset = trigger.get('offset', '')
            trigger_desc = f"At sunset{' ' + offset if offset else ''}"
        elif trigger_type == 'sunrise':
            offset = trigger.get('offset', '')
            trigger_desc = f"At sunrise{' ' + offset if offset else ''}"

        action_count = len(actions) if isinstance(actions, list) else 1
        action_desc = f"{action_count} action{'s' if action_count > 1 else ''}"

        return f"{trigger_desc}, {action_desc}"


# =============================================================================
# System Alerts
# =============================================================================

class Alert(Base):
    """
    System alerts for monitoring and notifications.

    Used for:
    - Stuck sensor detection
    - Service health issues
    - System warnings
    - User-defined alerts
    """
    __tablename__ = 'alerts'

    id = Column(Integer, primary_key=True)

    # Alert identification
    alert_type = Column(String(50), nullable=False)  # 'stuck_sensor', 'service_down', 'threshold_exceeded', etc.
    severity = Column(String(20), default='warning', nullable=False)  # 'info', 'warning', 'error', 'critical'

    # Alert content
    title = Column(String(255), nullable=False)
    message = Column(Text, nullable=False)

    # Related entity (optional)
    entity_id = Column(String(255))  # e.g., 'binary_sensor.kitchen_motion'
    entity_type = Column(String(50))  # e.g., 'sensor', 'service', 'device'

    # Alert data (flexible JSON for additional context)
    alert_data = Column(JSONB, default={})  # e.g., {'last_changed': '...', 'expected_interval': 3600}

    # Status tracking
    status = Column(String(20), default='active', nullable=False)  # 'active', 'acknowledged', 'resolved', 'dismissed'

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    acknowledged_at = Column(DateTime(timezone=True))
    resolved_at = Column(DateTime(timezone=True))

    # User actions
    acknowledged_by_id = Column(Integer, ForeignKey('users.id'))
    resolved_by_id = Column(Integer, ForeignKey('users.id'))
    resolution_notes = Column(Text)

    # Deduplication - prevent duplicate alerts for same issue
    dedup_key = Column(String(255), unique=True)  # e.g., 'stuck_sensor:binary_sensor.kitchen_motion'

    # Relationships
    acknowledged_by = relationship('User', foreign_keys=[acknowledged_by_id])
    resolved_by = relationship('User', foreign_keys=[resolved_by_id])

    __table_args__ = (
        Index('idx_alerts_status', 'status'),
        Index('idx_alerts_type', 'alert_type'),
        Index('idx_alerts_severity', 'severity'),
        Index('idx_alerts_entity', 'entity_id'),
        Index('idx_alerts_created', 'created_at'),
        Index('idx_alerts_dedup', 'dedup_key'),
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            'id': self.id,
            'alert_type': self.alert_type,
            'severity': self.severity,
            'title': self.title,
            'message': self.message,
            'entity_id': self.entity_id,
            'entity_type': self.entity_type,
            'alert_data': self.alert_data,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'acknowledged_at': self.acknowledged_at.isoformat() if self.acknowledged_at else None,
            'resolved_at': self.resolved_at.isoformat() if self.resolved_at else None,
            'acknowledged_by': self.acknowledged_by.username if self.acknowledged_by else None,
            'resolved_by': self.resolved_by.username if self.resolved_by else None,
            'resolution_notes': self.resolution_notes,
            'dedup_key': self.dedup_key,
        }


# =============================================================================
# Follow-Me Audio Configuration
# =============================================================================

class FollowMeConfig(Base):
    """
    Follow-me audio configuration.

    Controls how music playback follows users between rooms
    based on motion sensor detection.
    """
    __tablename__ = 'follow_me_config'

    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean, default=True)
    mode = Column(String(20), default='single')  # off, single, party
    debounce_seconds = Column(Float, default=5.0)
    grace_period_seconds = Column(Float, default=30.0)
    min_motion_duration_seconds = Column(Float, default=2.0)
    quiet_hours_start = Column(Integer, default=23)  # Hour in 24h format
    quiet_hours_end = Column(Integer, default=7)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'enabled': self.enabled,
            'mode': self.mode,
            'debounce_seconds': self.debounce_seconds,
            'grace_period_seconds': self.grace_period_seconds,
            'min_motion_duration_seconds': self.min_motion_duration_seconds,
            'quiet_hours_start': self.quiet_hours_start,
            'quiet_hours_end': self.quiet_hours_end,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class RoomMotionSensor(Base):
    """
    Maps rooms to their motion sensor entities.

    Enables follow-me audio to detect which room has activity.
    """
    __tablename__ = 'room_motion_sensors'

    id = Column(Integer, primary_key=True)
    room_name = Column(String(100), nullable=False, unique=True)
    motion_entity_id = Column(String(255), nullable=False)
    enabled = Column(Boolean, default=True)
    priority = Column(Integer, default=0)  # Higher priority rooms preferred
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'room_name': self.room_name,
            'motion_entity_id': self.motion_entity_id,
            'enabled': self.enabled,
            'priority': self.priority,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class FollowMeExcludedRoom(Base):
    """
    Rooms excluded from follow-me audio transfers.
    """
    __tablename__ = 'follow_me_excluded_rooms'

    id = Column(Integer, primary_key=True)
    room_name = Column(String(100), nullable=False, unique=True)
    reason = Column(String(255))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'room_name': self.room_name,
            'reason': self.reason,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# =============================================================================
# Model Configuration
# =============================================================================

class ModelConfiguration(Base):
    """
    Dynamic LLM model configurations with Ollama/MLX options.

    Stores per-model settings including:
    - Core options: temperature, max_tokens, timeout
    - Ollama options: num_ctx, num_batch, mirostat, top_k, top_p, etc.
    - MLX options: max_kv_size, quantization, etc.

    Use model_name="_default" for fallback configuration.
    """
    __tablename__ = 'model_configurations'

    id = Column(Integer, primary_key=True)
    model_name = Column(String(100), nullable=False, unique=True, index=True)
    display_name = Column(String(200))
    backend_type = Column(String(20), nullable=False, default='ollama')  # ollama, mlx, auto
    enabled = Column(Boolean, nullable=False, default=True)

    # Core settings
    temperature = Column(Numeric(3, 2), default=0.7)
    max_tokens = Column(Integer, default=2048)
    timeout_seconds = Column(Integer, default=60)
    keep_alive_seconds = Column(Integer, default=-1)

    # Extended options (JSONB for flexibility)
    ollama_options = Column(JSONB, default={})
    mlx_options = Column(JSONB, default={})

    # Metadata
    description = Column(Text)
    priority = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index('idx_model_configurations_enabled', 'enabled'),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'model_name': self.model_name,
            'display_name': self.display_name,
            'backend_type': self.backend_type,
            'enabled': self.enabled,
            'temperature': float(self.temperature) if self.temperature else 0.7,
            'max_tokens': self.max_tokens,
            'timeout_seconds': self.timeout_seconds,
            'keep_alive_seconds': self.keep_alive_seconds,
            'ollama_options': self.ollama_options or {},
            'mlx_options': self.mlx_options or {},
            'description': self.description,
            'priority': self.priority,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


# =============================================================================
# Service Usage Tracking
# =============================================================================

class ServiceUsage(Base):
    """
    Track monthly API usage for services with budget limits.

    Used for services like Bright Data that have monthly quotas.
    Tracks request counts per service per month for budget management.
    """
    __tablename__ = 'service_usage'

    id = Column(Integer, primary_key=True)
    service_name = Column(String(100), nullable=False, index=True)
    month = Column(String(7), nullable=False)  # YYYY-MM format
    request_count = Column(Integer, default=0)
    monthly_limit = Column(Integer, nullable=True)  # NULL = unlimited
    last_updated = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint('service_name', 'month', name='uq_service_month'),
        Index('ix_service_usage_service_month', 'service_name', 'month'),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'service_name': self.service_name,
            'month': self.month,
            'request_count': self.request_count,
            'monthly_limit': self.monthly_limit,
            'remaining': (self.monthly_limit - self.request_count) if self.monthly_limit else None,
            'last_updated': self.last_updated.isoformat() if self.last_updated else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# =============================================================================
# Model Downloads (Hugging Face Integration)
# =============================================================================

class ModelDownload(Base):
    """
    Track Hugging Face model downloads.

    Supports downloading GGUF (Ollama) and MLX (Apple Silicon) models
    from Hugging Face Hub with progress tracking and Ollama import.
    """
    __tablename__ = 'model_downloads'

    id = Column(Integer, primary_key=True)

    # Model identification
    repo_id = Column(String(255), nullable=False, index=True)  # "TheBloke/Llama-2-7B-GGUF"
    filename = Column(String(255), nullable=False)  # "llama-2-7b.Q4_K_M.gguf"
    model_format = Column(String(32), nullable=False)  # "gguf", "mlx"
    quantization = Column(String(32))  # "Q4_K_M", "Q8_0", etc.

    # Download metadata
    file_size_bytes = Column(Integer)  # Use Integer for SQLite compatibility
    download_path = Column(Text)

    # Status tracking
    status = Column(String(32), nullable=False, default='pending')  # pending, downloading, processing, completed, failed, cancelled
    progress_percent = Column(Float, default=0)
    downloaded_bytes = Column(Integer, default=0)
    error_message = Column(Text)

    # Ollama integration
    ollama_model_name = Column(String(255))
    ollama_imported = Column(Boolean, default=False)

    # Audit
    created_by_id = Column(Integer, ForeignKey('users.id'))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    started_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))

    # Relationships
    created_by = relationship('User', foreign_keys=[created_by_id])

    __table_args__ = (
        UniqueConstraint('repo_id', 'filename', name='uq_model_download_repo_file'),
        Index('idx_model_downloads_status', 'status'),
        Index('idx_model_downloads_format', 'model_format'),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'repo_id': self.repo_id,
            'filename': self.filename,
            'model_format': self.model_format,
            'quantization': self.quantization,
            'file_size_bytes': self.file_size_bytes,
            'download_path': self.download_path,
            'status': self.status,
            'progress_percent': self.progress_percent,
            'downloaded_bytes': self.downloaded_bytes,
            'error_message': self.error_message,
            'ollama_model_name': self.ollama_model_name,
            'ollama_imported': self.ollama_imported,
            'created_by_id': self.created_by_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
        }


# =============================================================================
# Cloud LLM Provider Support Models
# =============================================================================

class CloudLLMUsage(Base):
    """
    Tracks all cloud LLM API calls with full metadata for cost and usage analytics.

    Records every request to cloud providers (OpenAI, Anthropic, Google) with:
    - Token usage from provider metadata (not estimates)
    - Cost calculation from current pricing
    - Performance metrics (latency, TTFT)
    - Request context for analytics

    Open Source Compatible: Standard SQLAlchemy types.
    """
    __tablename__ = 'cloud_llm_usage'

    id = Column(Integer, primary_key=True)

    # Provider and model identification
    provider = Column(String(32), nullable=False, index=True)  # 'openai', 'anthropic', 'google'
    model = Column(String(100), nullable=False, index=True)

    # Token usage (from provider metadata, not estimates)
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)

    # Cost tracking (in USD, calculated from provider pricing)
    cost_usd = Column(Numeric(10, 6), nullable=False, default=0.0)

    # Performance metrics
    latency_ms = Column(Integer)
    ttft_ms = Column(Integer)  # Time to first token (streaming)
    streaming = Column(Boolean, default=False)

    # Request context
    request_id = Column(String(64), index=True)
    session_id = Column(String(64), index=True)
    user_id = Column(String(64), index=True)
    zone = Column(String(50))
    intent = Column(String(100))

    # Routing metadata
    was_fallback = Column(Boolean, default=False)
    fallback_reason = Column(String(255))

    # Timestamp
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    __table_args__ = (
        Index('idx_cloud_llm_usage_provider_time', 'provider', 'timestamp'),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'provider': self.provider,
            'model': self.model,
            'input_tokens': self.input_tokens,
            'output_tokens': self.output_tokens,
            'cost_usd': float(self.cost_usd) if self.cost_usd else 0.0,
            'latency_ms': self.latency_ms,
            'ttft_ms': self.ttft_ms,
            'streaming': self.streaming,
            'request_id': self.request_id,
            'session_id': self.session_id,
            'user_id': self.user_id,
            'zone': self.zone,
            'intent': self.intent,
            'was_fallback': self.was_fallback,
            'fallback_reason': self.fallback_reason,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
        }


class CloudLLMProvider(Base):
    """
    Configuration for cloud LLM providers (OpenAI, Anthropic, Google).

    Stores provider-level settings separate from API keys (which are in external_api_keys).
    """
    __tablename__ = 'cloud_llm_providers'

    id = Column(Integer, primary_key=True)

    # Provider identification
    provider = Column(String(32), unique=True, nullable=False, index=True)
    display_name = Column(String(100), nullable=False)

    # Status
    enabled = Column(Boolean, default=False, nullable=False)

    # Default configuration
    default_model = Column(String(100))
    max_tokens_default = Column(Integer, default=2048)
    temperature_default = Column(Numeric(3, 2), default=0.7)

    # Rate limiting
    rate_limit_rpm = Column(Integer, default=60)

    # Cost configuration (per 1M tokens in USD)
    input_cost_per_1m = Column(Numeric(10, 4))
    output_cost_per_1m = Column(Numeric(10, 4))

    # Health tracking
    last_health_check = Column(DateTime(timezone=True))
    health_status = Column(String(32), default='unknown')
    consecutive_failures = Column(Integer, default=0)

    # Metadata
    description = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'provider': self.provider,
            'display_name': self.display_name,
            'enabled': self.enabled,
            'default_model': self.default_model,
            'max_tokens_default': self.max_tokens_default,
            'temperature_default': float(self.temperature_default) if self.temperature_default else 0.7,
            'rate_limit_rpm': self.rate_limit_rpm,
            'input_cost_per_1m': float(self.input_cost_per_1m) if self.input_cost_per_1m else None,
            'output_cost_per_1m': float(self.output_cost_per_1m) if self.output_cost_per_1m else None,
            'last_health_check': self.last_health_check.isoformat() if self.last_health_check else None,
            'health_status': self.health_status,
            'consecutive_failures': self.consecutive_failures,
            'description': self.description,
        }


class CloudLLMModelPricing(Base):
    """
    Per-model pricing for accurate cost calculation.

    Stores pricing for each specific model ID to enable accurate cost tracking
    as providers update their pricing.
    """
    __tablename__ = 'cloud_llm_model_pricing'

    id = Column(Integer, primary_key=True)

    # Model identification
    provider = Column(String(32), nullable=False, index=True)
    model_id = Column(String(100), nullable=False)  # Exact model ID
    model_name = Column(String(100))  # Friendly name

    # Pricing (per 1M tokens in USD)
    input_cost_per_1m = Column(Numeric(10, 4), nullable=False)
    output_cost_per_1m = Column(Numeric(10, 4), nullable=False)

    # Capabilities
    max_context_length = Column(Integer)
    supports_vision = Column(Boolean, default=False)
    supports_tools = Column(Boolean, default=True)
    supports_streaming = Column(Boolean, default=True)

    # Metadata
    effective_date = Column(Date, default=func.current_date())
    deprecated = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint('provider', 'model_id', name='uq_cloud_llm_model_provider_model'),
        Index('idx_cloud_llm_model_pricing_provider', 'provider'),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'provider': self.provider,
            'model_id': self.model_id,
            'model_name': self.model_name,
            'input_cost_per_1m': float(self.input_cost_per_1m) if self.input_cost_per_1m else 0,
            'output_cost_per_1m': float(self.output_cost_per_1m) if self.output_cost_per_1m else 0,
            'max_context_length': self.max_context_length,
            'supports_vision': self.supports_vision,
            'supports_tools': self.supports_tools,
            'supports_streaming': self.supports_streaming,
            'effective_date': self.effective_date.isoformat() if self.effective_date else None,
            'deprecated': self.deprecated,
        }

    @classmethod
    def calculate_cost(cls, input_tokens: int, output_tokens: int,
                       input_cost_per_1m: float, output_cost_per_1m: float) -> float:
        """Calculate cost in USD from token counts and pricing."""
        input_cost = (input_tokens / 1_000_000) * input_cost_per_1m
        output_cost = (output_tokens / 1_000_000) * output_cost_per_1m
        return input_cost + output_cost


class RAGServiceBypass(Base):
    """
    Configuration for bypassing RAG services to cloud LLMs.

    Allows specific services (like recipes, websearch) to be routed directly
    to cloud LLMs instead of using dedicated local RAG services.
    """
    __tablename__ = 'rag_service_bypass'

    id = Column(Integer, primary_key=True)
    service_name = Column(String(50), unique=True, nullable=False)
    display_name = Column(String(100))
    bypass_enabled = Column(Boolean, default=False)

    # Cloud LLM Configuration
    cloud_provider = Column(String(32))  # 'openai', 'anthropic', 'google', or NULL
    cloud_model = Column(String(100))

    # Custom Instructions
    system_prompt = Column(Text)

    # Conditions
    bypass_conditions = Column(JSONB, default={})

    # Performance Settings
    temperature = Column(Numeric(3, 2), default=0.7)
    max_tokens = Column(Integer, default=1024)

    # Metadata
    description = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    created_by_id = Column(Integer, ForeignKey('users.id'))

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'service_name': self.service_name,
            'display_name': self.display_name,
            'bypass_enabled': self.bypass_enabled,
            'cloud_provider': self.cloud_provider,
            'cloud_model': self.cloud_model,
            'system_prompt': self.system_prompt,
            'bypass_conditions': self.bypass_conditions or {},
            'temperature': float(self.temperature) if self.temperature else 0.7,
            'max_tokens': self.max_tokens,
            'description': self.description,
        }


class EscalationPreset(Base):
    """
    Escalation presets (profiles) for model escalation behavior.

    Each preset contains a set of rules that determine when to escalate
    from smaller/faster models to larger/smarter models.
    Only one preset can be active at a time.
    """
    __tablename__ = 'escalation_presets'

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, unique=True, index=True)
    description = Column(Text)
    is_active = Column(Boolean, nullable=False, default=False, index=True)
    auto_activate_conditions = Column(JSONB)  # time_range, user_mode, etc.
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationship to rules
    rules = relationship('EscalationRule', back_populates='preset', cascade='all, delete-orphan')

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'is_active': self.is_active,
            'auto_activate_conditions': self.auto_activate_conditions,
            'rules_count': len(self.rules) if self.rules else 0,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }


class EscalationRule(Base):
    """
    Individual escalation rules belonging to a preset.

    Rules are evaluated in priority order (higher priority first).
    First matching rule triggers escalation.
    """
    __tablename__ = 'escalation_rules'

    id = Column(Integer, primary_key=True)
    preset_id = Column(Integer, ForeignKey('escalation_presets.id', ondelete='CASCADE'), nullable=False, index=True)
    rule_name = Column(String(100), nullable=False)
    trigger_type = Column(String(50), nullable=False, index=True)
    trigger_patterns = Column(JSONB, nullable=False)
    escalation_target = Column(String(20), nullable=False)  # 'complex' or 'super_complex'
    escalation_duration = Column(Integer, nullable=False, default=5)  # turns to stay escalated
    priority = Column(Integer, nullable=False, default=100)
    enabled = Column(Boolean, nullable=False, default=True, index=True)
    description = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationship to preset
    preset = relationship('EscalationPreset', back_populates='rules')

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'preset_id': self.preset_id,
            'rule_name': self.rule_name,
            'trigger_type': self.trigger_type,
            'trigger_patterns': self.trigger_patterns,
            'escalation_target': self.escalation_target,
            'escalation_duration': self.escalation_duration,
            'priority': self.priority,
            'enabled': self.enabled,
            'description': self.description,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }


class EscalationState(Base):
    """
    Tracks current escalation state per session.

    When a session is escalated, this tracks what level it's at
    and how many turns remain before dropping back.
    Also tracks manual overrides for testing/debugging.
    """
    __tablename__ = 'escalation_state'

    id = Column(Integer, primary_key=True)
    session_id = Column(String(255), nullable=False, unique=True, index=True)
    escalated_to = Column(String(20), nullable=False)  # 'complex' or 'super_complex'
    triggered_by_rule_id = Column(Integer, ForeignKey('escalation_rules.id', ondelete='SET NULL'))
    turns_remaining = Column(Integer, nullable=True)  # Nullable for time-based overrides
    is_manual_override = Column(Boolean, nullable=False, default=False, index=True)
    override_reason = Column(Text, nullable=True)
    escalated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at = Column(DateTime(timezone=True))

    # Relationship
    triggered_by_rule = relationship('EscalationRule')

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'session_id': self.session_id,
            'escalated_to': self.escalated_to,
            'triggered_by_rule_id': self.triggered_by_rule_id,
            'turns_remaining': self.turns_remaining,
            'is_manual_override': self.is_manual_override,
            'override_reason': self.override_reason,
            'escalated_at': self.escalated_at.isoformat() if self.escalated_at else None,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None
        }

    def is_expired(self) -> bool:
        """Check if this escalation state has expired."""
        from datetime import datetime
        if self.expires_at and datetime.utcnow() > self.expires_at:
            return True
        if self.turns_remaining is not None and self.turns_remaining <= 0:
            return True
        return False


class EscalationEvent(Base):
    """
    Audit log for escalation events.

    Records every escalation, de-escalation, and manual override
    for analytics and debugging. Retained for 90 days by default.
    """
    __tablename__ = 'escalation_events'

    id = Column(Integer, primary_key=True)
    session_id = Column(String(255), nullable=False, index=True)
    event_type = Column(String(50), nullable=False, index=True)  # 'escalation', 'de-escalation', 'manual_override', 'override_cancelled'
    from_model = Column(String(20), nullable=True)  # NULL for initial state
    to_model = Column(String(20), nullable=False)
    triggered_by_rule_id = Column(Integer, ForeignKey('escalation_rules.id', ondelete='SET NULL'))
    triggered_by_user = Column(String(100), nullable=True)  # For manual overrides
    preset_id = Column(Integer, ForeignKey('escalation_presets.id', ondelete='SET NULL'))
    preset_name = Column(String(100), nullable=True)  # Denormalized for query convenience
    trigger_context = Column(JSONB, nullable=True)  # Query, response snippet, match details
    reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    # Relationships
    triggered_by_rule = relationship('EscalationRule')
    preset = relationship('EscalationPreset')

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'session_id': self.session_id,
            'event_type': self.event_type,
            'from_model': self.from_model,
            'to_model': self.to_model,
            'triggered_by_rule_id': self.triggered_by_rule_id,
            'triggered_by_user': self.triggered_by_user,
            'preset_id': self.preset_id,
            'preset_name': self.preset_name,
            'trigger_context': self.trigger_context,
            'reason': self.reason,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class IntentRoutingConfig(Base):
    """
    Per-intent routing strategy configuration.

    Enables hybrid cascading fallback system where each intent can be configured to:
    - cascading: Direct RAG first, fallback to tool calling on failure (default)
    - always_tool_calling: Skip direct RAG, always use LLM tool selection
    - direct_only: Never fall back to tool calling
    """
    __tablename__ = 'intent_routing_config'

    id = Column(Integer, primary_key=True)
    intent_name = Column(String(50), unique=True, nullable=False, index=True)
    display_name = Column(String(100), nullable=False)
    routing_strategy = Column(String(20), nullable=False, default='cascading')
    enabled = Column(Boolean, default=True)
    priority = Column(Integer, default=10)
    config = Column(JSONB, default={})
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def to_dict(self):
        return {
            'id': self.id,
            'intent_name': self.intent_name,
            'display_name': self.display_name,
            'routing_strategy': self.routing_strategy,
            'enabled': self.enabled,
            'priority': self.priority,
            'config': self.config or {},
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }
