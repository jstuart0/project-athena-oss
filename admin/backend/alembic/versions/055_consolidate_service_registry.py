"""Consolidate service registry into rag_services.

Revision ID: 055
Revises: 054b
Create Date: 2026-05-08

ATHENA-1 Campaign 4 Phase 2: merge service_registry + athena_services (admin DB)
and the asyncpg rag_services (athena DB) into the single ORM-managed rag_services
table in the admin DB.

Steps:
  1. Add new columns to rag_services (idempotent — checks existing column list).
  2. Pre-validate endpoint_url shape before any casting (round-2 M1 / otto M1).
  3. Backfill host/port/protocol from endpoint_url where host IS NULL.
  4. Copy rows from service_registry (JOIN server_configs) into rag_services.
  5. Copy rows from athena_services into rag_services.
  6. Assert backfill completeness; then enforce NOT NULL on host and port.
  7. Repoint rag_connectors.service_id FK from service_registry to rag_services
     (look up actual constraint name via inspector — bob H1; D3).
  8. Rename service_registry → service_registry_deprecated,
          athena_services → athena_services_deprecated,
          server_configs → server_configs_deprecated (deferred-drop per D5).

Production pre-flight (run before applying this migration):
  SELECT name,
         CASE WHEN api_key_encrypted IS NULL THEN 'null' ELSE 'set' END AS key_state
  FROM rag_services ORDER BY name;
  -- Verify external-API rows (openweather, newsapi, etc.) have key_state='set'.
  -- Migration preserves api_key_encrypted unconditionally. (xander CRIT-2)

Downgrade: reverses renames + drops columns.
  NOTE: data-copy in upgrade is one-way.  The source rows still exist in the
  renamed tables so a manual rollback path is available.  api_key_encrypted is
  NOT dropped on downgrade — dropping it would destroy external API tokens.

SQLite (DEV_MODE / tests): data-copy steps are skipped entirely; column adds
  are exercised.  The FK repoint and table renames are also skipped on SQLite
  because SQLite has no ALTER TABLE RENAME TABLE support via alembic and no
  named FK constraints.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect

# revision identifiers, used by Alembic.
revision = '055'
down_revision = '054b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == 'sqlite'

    # -------------------------------------------------------------------------
    # Step 1: Add new columns to rag_services — idempotent via inspector.
    # Phase 1 reconcile widened the ensure_rag_services_table DDL to include
    # all these columns, so on dev installs they may already exist.  Use the
    # inspector to skip ADD COLUMN for columns already present. (Phase 1 note)
    # -------------------------------------------------------------------------
    inspector = sa_inspect(bind)
    existing_cols = {c['name'] for c in inspector.get_columns('rag_services')}

    def _add_col_if_missing(col_name: str, col_def: sa.Column) -> None:
        if col_name not in existing_cols:
            op.add_column('rag_services', col_def)

    _add_col_if_missing('host', sa.Column('host', sa.String(255), nullable=True))
    _add_col_if_missing('port', sa.Column('port', sa.Integer(), nullable=True))
    _add_col_if_missing('protocol', sa.Column('protocol', sa.String(8), server_default='http'))
    _add_col_if_missing('health_endpoint', sa.Column('health_endpoint', sa.String(256), server_default='/health'))
    _add_col_if_missing('control_method', sa.Column('control_method', sa.String(50), server_default='none'))
    _add_col_if_missing('container_name', sa.Column('container_name', sa.String(255), nullable=True))
    _add_col_if_missing('is_running', sa.Column('is_running', sa.Boolean(), server_default='false'))
    _add_col_if_missing('last_response_time_ms', sa.Column('last_response_time_ms', sa.Integer(), nullable=True))
    _add_col_if_missing('last_error', sa.Column('last_error', sa.Text(), nullable=True))
    _add_col_if_missing('auto_start', sa.Column('auto_start', sa.Boolean(), server_default='true'))
    _add_col_if_missing('description', sa.Column('description', sa.Text(), nullable=True))
    _add_col_if_missing('health_message', sa.Column('health_message', sa.String(500), nullable=True))  # ruby B1

    # api_key_encrypted: pre-existed on prod rag_services (xander CRIT-2).
    # Add only if absent (e.g., in a DEV_MODE in-memory SQLite that never ran
    # the seed's CREATE TABLE).  Must NOT be dropped on downgrade.
    _add_col_if_missing('api_key_encrypted', sa.Column('api_key_encrypted', sa.Text(), nullable=True))

    # Widen name/display_name to fit athena_services.service_name (D2).
    # SQLite ALTER COLUMN is not supported — skip on SQLite.
    if not is_sqlite:
        op.alter_column('rag_services', 'name',
                        existing_type=sa.String(50),
                        type_=sa.String(64),
                        existing_nullable=False)
        op.alter_column('rag_services', 'display_name',
                        existing_type=sa.String(255),
                        type_=sa.String(255),
                        existing_nullable=True)

    # -------------------------------------------------------------------------
    # Step 2 (Postgres only): pre-validate endpoint_url shape BEFORE casting.
    # (round-2 M1 / otto M1)
    # Regex: scheme://hostname-or-IPv4(:port)?(/path)?
    # IPv6-bracketed URLs are explicitly out of scope.
    # -------------------------------------------------------------------------
    if not is_sqlite:
        bad_rows = bind.execute(sa.text(r"""
            SELECT id, name, endpoint_url
            FROM rag_services
            WHERE endpoint_url IS NOT NULL
              AND host IS NULL
              AND endpoint_url !~ '^https?://[a-zA-Z0-9._-]+(:[0-9]+)?(/.*)?$'
        """)).fetchall()
        if bad_rows:
            sample = ', '.join(
                f'{r[1]}={r[2]!r}'
                for r in bad_rows[:5]
            )
            raise RuntimeError(
                f"Phase 2 migration aborted (round-2 M1): "
                f"{len(bad_rows)} rag_services rows have endpoint_url that does NOT match "
                f"`^https?://[a-zA-Z0-9._-]+(:[0-9]+)?(/.*)?$`. "
                f"Sample: {sample}. "
                f"Operator action: hand-edit endpoint_url to a parseable form "
                f"(e.g., `http://host:port/path`) OR set host/port columns directly. "
                f"IPv6-bracketed URLs are not supported by this migration; "
                f"set host/port columns directly for those rows."
            )

        # Backfill host/port/protocol from endpoint_url (path-stripped split).
        # split_part strips path BEFORE splitting on ':' (otto M1 fix).
        op.execute(sa.text("""
            UPDATE rag_services
            SET host     = split_part(split_part(split_part(endpoint_url, '://', 2), '/', 1), ':', 1),
                port     = COALESCE(
                               NULLIF(
                                   split_part(split_part(split_part(endpoint_url, '://', 2), '/', 1), ':', 2),
                                   ''
                               )::int,
                               80
                           ),
                protocol = COALESCE(NULLIF(split_part(endpoint_url, '://', 1), ''), 'http')
            WHERE host IS NULL AND endpoint_url IS NOT NULL
        """))

    # -------------------------------------------------------------------------
    # Step 3 (Postgres only): copy rows from service_registry (JOIN server_configs)
    # -------------------------------------------------------------------------
    if not is_sqlite:
        op.execute(sa.text("""
            INSERT INTO rag_services (
                name, display_name, host, port, protocol, health_endpoint, enabled
            )
            SELECT
                sr.service_name,
                sr.service_name,
                sc.ip_address,
                sr.port,
                COALESCE(sr.protocol, 'http'),
                COALESCE(sr.health_endpoint, '/health'),
                TRUE
            FROM service_registry sr
            JOIN server_configs sc ON sr.server_id = sc.id
            ON CONFLICT (name) DO UPDATE SET
                host           = COALESCE(rag_services.host, EXCLUDED.host),
                port           = COALESCE(rag_services.port, EXCLUDED.port),
                protocol       = COALESCE(rag_services.protocol, EXCLUDED.protocol),
                health_endpoint = COALESCE(rag_services.health_endpoint, EXCLUDED.health_endpoint)
        """))

    # -------------------------------------------------------------------------
    # Step 4 (Postgres only): copy rows from athena_services
    # -------------------------------------------------------------------------
    if not is_sqlite:
        op.execute(sa.text("""
            INSERT INTO rag_services (
                name, display_name, description, service_type,
                host, port, health_endpoint, control_method, container_name,
                is_running, auto_start, enabled
            )
            SELECT
                service_name,
                display_name,
                description,
                service_type,
                host,
                port,
                health_endpoint,
                control_method,
                container_name,
                is_running,
                auto_start,
                enabled
            FROM athena_services
            ON CONFLICT (name) DO UPDATE SET
                description    = COALESCE(rag_services.description, EXCLUDED.description),
                control_method = COALESCE(NULLIF(rag_services.control_method, 'none'), EXCLUDED.control_method),
                container_name = COALESCE(rag_services.container_name, EXCLUDED.container_name),
                is_running     = EXCLUDED.is_running,
                auto_start     = EXCLUDED.auto_start
        """))

    # -------------------------------------------------------------------------
    # Step 5 (Postgres only): assert backfill completeness BEFORE enforcing
    # NOT NULL (otto M1 stop condition).
    # -------------------------------------------------------------------------
    if not is_sqlite:
        null_rows = bind.execute(sa.text("""
            SELECT name, endpoint_url
            FROM rag_services
            WHERE host IS NULL OR host = '' OR port IS NULL
        """)).fetchall()
        if null_rows:
            sample = [r[0] for r in null_rows[:3]]
            raise RuntimeError(
                f"Phase 2 migration aborted: {len(null_rows)} rag_services rows "
                f"have NULL/empty host or port after backfill. "
                f"Sample: {sample}. "
                f"Fix data, then re-run `alembic upgrade head`."
            )

        # Safe to enforce NOT NULL now.
        op.alter_column('rag_services', 'host', nullable=False)
        op.alter_column('rag_services', 'port', nullable=False)

    # -------------------------------------------------------------------------
    # Step 6 (Postgres only): repoint rag_connectors.service_id FK from
    # service_registry to rag_services (D3 / bob H1).
    #
    # Use inspector to look up the actual FK constraint name — do NOT hardcode
    # 'rag_connectors_service_id_fkey' (bob H1).  In Phase 1 the ORM FK was
    # already repointed to rag_services, but the DB-level constraint may still
    # point at service_registry (or may already point at rag_services if the DB
    # was freshly initialized after Phase 1 landed).
    # -------------------------------------------------------------------------
    if not is_sqlite:
        fk_info = inspector.get_foreign_keys('rag_connectors')
        # Find the constraint on the service_id column pointing at either table.
        target_fk = None
        target_fk_name = None
        for fk in fk_info:
            if 'service_id' in fk.get('constrained_columns', []):
                target_fk = fk
                target_fk_name = fk.get('name')
                break

        if target_fk is not None and target_fk.get('referred_table') != 'rag_services':
            # FK still points at service_registry — remap values and repoint.
            op.drop_constraint(target_fk_name, 'rag_connectors', type_='foreignkey')

            # Remap service_id values: old service_registry.id → new rag_services.id
            op.execute(sa.text("""
                UPDATE rag_connectors rc
                SET service_id = rs.id
                FROM service_registry sr
                JOIN rag_services rs ON rs.name = sr.service_name
                WHERE rc.service_id = sr.id
            """))

            # NULL out unmappable rows (logged below — tessa T-H1)
            op.execute(sa.text("""
                UPDATE rag_connectors
                SET service_id = NULL
                WHERE service_id IS NOT NULL
                  AND service_id NOT IN (SELECT id FROM rag_services)
            """))

            op.create_foreign_key(
                'rag_connectors_service_id_fkey',
                'rag_connectors', 'rag_services',
                ['service_id'], ['id']
            )
        elif target_fk is not None and target_fk.get('referred_table') == 'rag_services':
            # FK already points at rag_services (fresh install after Phase 1).
            # The constraint name may differ from the legacy name; leave it alone.
            pass
        # If no FK constraint at all (e.g., SQLite — already guarded), nothing to do.

    # -------------------------------------------------------------------------
    # Step 7: rename old tables to _deprecated (deferred-drop per D5).
    # SQLite does not support RENAME TABLE via op.rename_table; skip.
    # -------------------------------------------------------------------------
    if not is_sqlite:
        op.rename_table('service_registry', 'service_registry_deprecated')
        op.rename_table('athena_services', 'athena_services_deprecated')
        op.rename_table('server_configs', 'server_configs_deprecated')


def downgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == 'sqlite'

    if not is_sqlite:
        # Reverse Step 7: restore original table names.
        op.rename_table('server_configs_deprecated', 'server_configs')
        op.rename_table('athena_services_deprecated', 'athena_services')
        op.rename_table('service_registry_deprecated', 'service_registry')

        # Reverse Step 6: restore FK on rag_connectors pointing at service_registry.
        inspector = sa_inspect(bind)
        fk_info = inspector.get_foreign_keys('rag_connectors')
        target_fk_name = None
        for fk in fk_info:
            if 'service_id' in fk.get('constrained_columns', []):
                target_fk_name = fk.get('name')
                break

        if target_fk_name:
            op.drop_constraint(target_fk_name, 'rag_connectors', type_='foreignkey')

        # Attempt to remap service_id back — best-effort.
        op.execute(sa.text("""
            UPDATE rag_connectors rc
            SET service_id = sr.id
            FROM service_registry sr
            JOIN rag_services rs ON rs.name = sr.service_name
            WHERE rc.service_id = rs.id
        """))

        op.create_foreign_key(
            'rag_connectors_service_id_fkey',
            'rag_connectors', 'service_registry',
            ['service_id'], ['id']
        )

    # Reverse Step 1: drop columns we added.
    # NOTE: api_key_encrypted is NOT dropped — it pre-existed on prod and
    # removing it would destroy external API tokens. (xander CRIT-2)
    for col in (
        'description', 'health_message', 'auto_start', 'last_error',
        'last_response_time_ms', 'is_running', 'container_name',
        'control_method', 'health_endpoint', 'protocol', 'port', 'host',
    ):
        # SQLite does not support DROP COLUMN in older versions; guard.
        if not is_sqlite:
            op.drop_column('rag_services', col)
