"""
MIBSP Flask Application Factory
Creates and configures the Flask application with all extensions.
"""
from sqlalchemy import inspect, text
from flask import Flask, render_template
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect
from config import config

# Initialize extensions (no app yet)
db = SQLAlchemy()
migrate = Migrate()
csrf = CSRFProtect()


def create_app(config_name=None):
    """
    Application factory pattern - creates and configures Flask app.
    
    Args:
        config_name: Configuration environment (development, testing, production)
    
    Returns:
        Configured Flask application instance
    """
    if config_name is None:
        import os
        config_name = os.environ.get('FLASK_ENV', 'default')
    
    app = Flask(__name__, 
                instance_relative_config=True,
                template_folder='templates',
                static_folder='static')
    
    # Load configuration
    app.config.from_object(config[config_name])
    config[config_name].init_app(app)
    
    # Initialize extensions with app
    db.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)
    
    # Register blueprints
    from app.routes.public import public_bp
    from app.routes.auth import auth_bp
    from app.routes.officer import officer_bp
    from app.routes.admin import admin_bp
    
    app.register_blueprint(public_bp)
    app.register_blueprint(auth_bp, url_prefix='/auth')
    app.register_blueprint(officer_bp, url_prefix='/officer')
    app.register_blueprint(admin_bp, url_prefix='/admin')
    
    # Register error handlers
    register_error_handlers(app)
    
    # Register template filters
    register_template_filters(app)
    
    # Ensure schema compatibility and bootstrap tables where needed.
    # This keeps self-hosted Render/Postgres/MySQL instances in sync with
    # the latest model fields even when the DB was provisioned from older schema.
    with app.app_context():
        ensure_database_schema_compatibility(app)
    
    return app


def ensure_database_schema_compatibility(app):
    """
    Ensure required tables and columns exist across environments.

    Older deployments may have been created before newer model fields were added.
    SQLAlchemy `create_all()` won't alter existing schemas, so we explicitly
    add missing columns when tables already exist.
    """
    required_columns_by_table = {
        'departments': {},
        'services': {
            'sla_days': "INTEGER NOT NULL DEFAULT 7",
        },
        'users': {
            'failed_login_attempts': "INTEGER NOT NULL DEFAULT 0",
            'locked_until': "DATETIME",
        },
        'complaints': {
            'escalation_level': "INTEGER NOT NULL DEFAULT 0",
            'sla_due_at': "DATETIME",
            'delayed_at': "DATETIME",
            'reopen_count': "INTEGER NOT NULL DEFAULT 0",
            'citizen_rating': "INTEGER",
            'citizen_feedback': "TEXT",
            'feedback_submitted_at': "DATETIME",
            'priority': "VARCHAR(20) NOT NULL DEFAULT 'Normal'",
            'ai_category': "VARCHAR(80)",
            'ai_sentiment': "VARCHAR(20) NOT NULL DEFAULT 'neutral'",
            'ai_urgent': "BOOLEAN NOT NULL DEFAULT 0",
            'state': "VARCHAR(80)",
            'district': "VARCHAR(120)",
            'city': "VARCHAR(120)",
            'location_lat': "FLOAT",
            'location_lng': "FLOAT",
        },
        'audit_logs': {},
    }

    # Keep a compatibility path for local sqlite databases.
    sqlite_patch = {
        'services': {
            'sla_days': "INTEGER NOT NULL DEFAULT 7",
        },
        'users': {
            'failed_login_attempts': "INTEGER NOT NULL DEFAULT 0",
            'locked_until': "DATETIME",
        },
        'complaints': {
            'escalation_level': "INTEGER NOT NULL DEFAULT 0",
            'sla_due_at': "DATETIME",
            'delayed_at': "DATETIME",
            'reopen_count': "INTEGER NOT NULL DEFAULT 0",
            'citizen_rating': "INTEGER",
            'citizen_feedback': "TEXT",
            'feedback_submitted_at': "DATETIME",
            'priority': "VARCHAR(20) NOT NULL DEFAULT 'Normal'",
            'ai_category': "VARCHAR(80)",
            'ai_sentiment': "VARCHAR(20) NOT NULL DEFAULT 'neutral'",
            'ai_urgent': "BOOLEAN NOT NULL DEFAULT 0",
            'state': "VARCHAR(80)",
            'district': "VARCHAR(120)",
            'city': "VARCHAR(120)",
            'location_lat': "FLOAT",
            'location_lng': "FLOAT",
        },
    }

    try:
        inspector = inspect(db.engine)
        existing_tables = {table_name for table_name in inspector.get_table_names()}

        # Fresh DB: create all tables so basic app routes work immediately.
        if not {'departments', 'services', 'complaints', 'users', 'audit_logs'}.issubset(
            existing_tables
        ):
            db.create_all()
            existing_tables = {table_name for table_name in inspector.get_table_names()}

        # Build a stable set of column names for each table.
        existing_columns = {}
        for table in required_columns_by_table:
            if table not in existing_tables:
                continue
            cols = inspector.get_columns(table)
            existing_columns[table] = {c['name'] for c in cols}

        dialect = db.engine.dialect.name

        for table, columns in required_columns_by_table.items():
            if table not in existing_tables:
                continue

            if dialect == 'sqlite':
                patch = sqlite_patch.get(table, {})
            else:
                patch = {}
                for column_name, ddl in columns.items():
                    ddl = ddl.replace('DATETIME', 'TIMESTAMP')
                    if column_name == 'ai_urgent':
                        ddl = ddl.replace('DEFAULT 0', 'DEFAULT false')
                    patch[column_name] = ddl

            current_columns = existing_columns.get(table, set())
            for column_name, ddl in patch.items():
                if column_name in current_columns:
                    continue
                try:
                    db.session.execute(text(f'ALTER TABLE {table} ADD COLUMN {column_name} {ddl}'))
                    app.logger.warning(
                        'Applied runtime schema compatibility fix: %s.%s (%s)',
                        table, column_name, dialect
                    )
                except Exception as exc:
                    app.logger.error(
                        'Failed to add missing column %s.%s: %s',
                        table, column_name, str(exc)
                    )
                    raise

        db.session.commit()

        # Keep the existing SQLite compatibility routine for runtime safety.
        if db.engine.url.get_backend_name() == 'sqlite':
            ensure_sqlite_schema_compatibility(app)
    except Exception:
        db.session.rollback()
        app.logger.exception('Database schema compatibility check failed')
        raise


def ensure_sqlite_schema_compatibility(app):
    """
    Auto-upgrade SQLite schemas for development environments without migrations.
    Adds missing columns using ALTER TABLE when model fields evolve.
    """
    try:
        if db.engine.url.get_backend_name() != 'sqlite':
            return
    except Exception:
        return

    schema_patches = {
        'services': {
            'sla_days': "INTEGER NOT NULL DEFAULT 7",
        },
        'users': {
            'failed_login_attempts': "INTEGER NOT NULL DEFAULT 0",
            'locked_until': "DATETIME",
        },
        'complaints': {
            'escalation_level': "INTEGER NOT NULL DEFAULT 0",
            'sla_due_at': "DATETIME",
            'delayed_at': "DATETIME",
            'reopen_count': "INTEGER NOT NULL DEFAULT 0",
            'citizen_rating': "INTEGER",
            'citizen_feedback': "TEXT",
            'feedback_submitted_at': "DATETIME",
            'priority': "VARCHAR(20) NOT NULL DEFAULT 'Normal'",
            'ai_category': "VARCHAR(80)",
            'ai_sentiment': "VARCHAR(20) NOT NULL DEFAULT 'neutral'",
            'ai_urgent': "BOOLEAN NOT NULL DEFAULT 0",
            'state': "VARCHAR(80)",
            'district': "VARCHAR(120)",
            'city': "VARCHAR(120)",
            'location_lat': "FLOAT",
            'location_lng': "FLOAT",
        },
    }

    for table_name, columns in schema_patches.items():
        existing = db.session.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
        existing_columns = {row[1] for row in existing}

        for column_name, column_def in columns.items():
            if column_name in existing_columns:
                continue
            db.session.execute(
                text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")
            )
            app.logger.warning(
                "Applied SQLite dev schema patch: %s.%s", table_name, column_name
            )

    db.session.commit()


def register_error_handlers(app):
    """Register custom error handlers."""
    
    @app.errorhandler(403)
    def forbidden(error):
        return render_template('errors/403.html'), 403
    
    @app.errorhandler(404)
    def not_found(error):
        return render_template('errors/404.html'), 404
    
    @app.errorhandler(500)
    def internal_error(error):
        db.session.rollback()
        return render_template('errors/500.html'), 500
    
    @app.errorhandler(413)
    def too_large(error):
        return render_template('errors/413.html'), 413


def register_template_filters(app):
    """Register custom Jinja2 template filters."""
    
    @app.template_filter('format_datetime')
    def format_datetime(value, format='%d %b %Y, %I:%M %p'):
        """Format datetime for display."""
        if value is None:
            return 'N/A'
        return value.strftime(format)
    
    @app.template_filter('status_badge')
    def status_badge(status):
        """Return Bootstrap badge class for status."""
        badges = {
            'Pending': 'badge-pending',
            'Under Review': 'badge-review',
            'Action Taken': 'badge-action',
            'Delayed': 'badge-delayed',
            'Reopened': 'badge-reopened',
            'Closed': 'badge-closed'
        }
        return badges.get(status, 'badge-secondary')

    @app.template_filter('status_icon')
    def status_icon(status):
        """Return FontAwesome icon name for complaint status."""
        icons = {
            'Pending': 'clock',
            'Under Review': 'search',
            'Action Taken': 'tools',
            'Delayed': 'triangle-exclamation',
            'Reopened': 'rotate-left',
            'Closed': 'check-circle'
        }
        return icons.get(status, 'circle')
