"""
MIBSP Flask Application Factory
Creates and configures the Flask application with all extensions.
"""
from sqlalchemy import text, inspect
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
    
    # Keep DB schema aligned with current model fields across environments.
    # Production instances may have legacy schemas from older releases.
    with app.app_context():
        ensure_schema_compatibility(app, run_create_all=(config_name == 'development'))
    
    return app


def ensure_schema_compatibility(app, run_create_all=False):
    """
    Auto-upgrade database schemas when migrations are not available.
    Adds missing columns using ALTER TABLE in supported databases.
    """
    try:
        if run_create_all:
            db.create_all()
    except Exception:
        return

    schema_patches = {
        'services': {
            'sla_days': "INTEGER NOT NULL DEFAULT 7",
        },
        'users': {
            'failed_login_attempts': "INTEGER NOT NULL DEFAULT 0",
            'locked_until': "TIMESTAMP",
        },
        'complaints': {
            'escalation_level': "INTEGER NOT NULL DEFAULT 0",
            'sla_due_at': "TIMESTAMP",
            'delayed_at': "TIMESTAMP",
            'reopen_count': "INTEGER NOT NULL DEFAULT 0",
            'citizen_rating': "INTEGER",
            'citizen_feedback': "TEXT",
            'feedback_submitted_at': "TIMESTAMP",
            'priority': "VARCHAR(20) NOT NULL DEFAULT 'Normal'",
            'ai_category': "VARCHAR(80)",
            'ai_sentiment': "VARCHAR(20) NOT NULL DEFAULT 'neutral'",
            'ai_urgent': "BOOLEAN NOT NULL DEFAULT FALSE",
            'state': "VARCHAR(80)",
            'district': "VARCHAR(120)",
            'city': "VARCHAR(120)",
            'location_lat': "FLOAT",
            'location_lng': "FLOAT",
        },
    }

    try:
        inspector = inspect(db.engine)
        existing_tables = set(inspector.get_table_names())
    except Exception:
        app.logger.exception('Unable to inspect database schema.')
        return

    dialect = db.engine.url.get_backend_name()
    for table_name, columns in schema_patches.items():
        if table_name not in existing_tables:
            continue

        try:
            existing_columns = {
                column['name']
                for column in inspector.get_columns(table_name)
            }
        except Exception:
            app.logger.exception('Unable to inspect columns for table %s', table_name)
            continue

        for column_name, column_def in columns.items():
            if column_name in existing_columns:
                continue
            alter_sql = f'ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}'
            if dialect == 'postgresql':
                # Postgres supports IF NOT EXISTS for column additions.
                alter_sql = f'ALTER TABLE "{table_name}" ADD COLUMN IF NOT EXISTS {column_name} {column_def}'
            try:
                db.session.execute(text(alter_sql))
                existing_columns.add(column_name)
                app.logger.warning("Applied schema patch: %s.%s", table_name, column_name)
            except Exception as exc:
                db.session.rollback()
                error_text = str(exc).lower()
                if (
                    'duplicate column' in error_text
                    or 'already exists' in error_text
                ):
                    app.logger.info("Schema patch already present: %s.%s", table_name, column_name)
                    continue
                app.logger.exception(
                    "Schema patch failed for %s.%s", table_name, column_name
                )
                raise

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
