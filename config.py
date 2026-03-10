"""
MIBSP Configuration Module
Supports: Development (SQLite), VPS Production (MySQL), Render (PostgreSQL)
"""
import os
from datetime import timedelta
from dotenv import load_dotenv

basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, '.env'))


class Config:
    """Base configuration - shared across all environments."""
    
    # Application
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-in-production'
    
    # Database (will be overridden by subclasses)
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_pre_ping': True,
        'pool_recycle': 3600,
    }
    
    # File Uploads
    UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER') or os.path.join(basedir, 'uploads')
    MAX_CONTENT_LENGTH = int(os.environ.get('MAX_CONTENT_LENGTH', 16 * 1024 * 1024))
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'pdf'}
    
    # Session Security
    PERMANENT_SESSION_LIFETIME = timedelta(
        hours=int(os.environ.get('SESSION_LIFETIME_HOURS', 8))
    )
    SESSION_COOKIE_SECURE = False  # Override in production
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'

    # Geo heatmap performance guard (API limit before progressive rendering)
    GEO_HEATMAP_MAX_POINTS = int(os.environ.get('GEO_HEATMAP_MAX_POINTS', 2500))
    
    # CSRF
    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = 3600  # 1 hour
    
    # Pagination
    ITEMS_PER_PAGE = 20
    
    # Redis / Celery
    REDIS_URL = os.environ.get('REDIS_URL') or 'redis://localhost:6379/0'
    CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL') or 'redis://localhost:6379/0'
    CELERY_RESULT_BACKEND = os.environ.get('CELERY_RESULT_BACKEND') or 'redis://localhost:6379/1'
    
    # Email (Optional)
    MAIL_SERVER = os.environ.get('MAIL_SERVER')
    MAIL_PORT = int(os.environ.get('MAIL_PORT', 587))
    MAIL_USE_TLS = os.environ.get('MAIL_USE_TLS', 'true').lower() in ['true', 'on', '1']
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME')
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD')
    NOTIFICATION_TO_EMAIL = os.environ.get('NOTIFICATION_TO_EMAIL')

    # AI Assistant (Optional)
    OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
    OPENAI_MODEL = os.environ.get('OPENAI_MODEL', 'gpt-4o-mini')
    AI_RATE_MIN_INTERVAL_SECONDS = int(os.environ.get('AI_RATE_MIN_INTERVAL_SECONDS', 3))
    AI_RATE_WINDOW_SECONDS = int(os.environ.get('AI_RATE_WINDOW_SECONDS', 60))
    AI_RATE_MAX_REQUESTS_PER_WINDOW = int(os.environ.get('AI_RATE_MAX_REQUESTS_PER_WINDOW', 20))
    ADMIN_EMAIL_2FA_ENABLED = os.environ.get('ADMIN_EMAIL_2FA_ENABLED', 'false').lower() in ['true', 'on', '1']
    ADMIN_OTP_EXPIRY_MINUTES = int(os.environ.get('ADMIN_OTP_EXPIRY_MINUTES', 5))
    ADMIN_OTP_LENGTH = int(os.environ.get('ADMIN_OTP_LENGTH', 6))

    # Runtime performance guard for SLA recalculation on read-heavy pages
    SLA_CHECK_INTERVAL_SECONDS = int(os.environ.get('SLA_CHECK_INTERVAL_SECONDS', 20))
    
    @staticmethod
    def init_app(app):
        """Initialize application with this config."""
        # Ensure upload directory exists
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
        
        # Ensure instance directory exists
        os.makedirs(app.instance_path, exist_ok=True)


class DevelopmentConfig(Config):
    """Development configuration with SQLite."""
    DEBUG = True
    
    SQLALCHEMY_DATABASE_URI = os.environ.get('DEV_DATABASE_URL') or \
        'sqlite:///' + os.path.join(basedir, 'instance', 'mibsp_dev.db')
    
    @classmethod
    def init_app(cls, app):
        super().init_app(app)
        app.config['SESSION_COOKIE_SECURE'] = False


class TestingConfig(Config):
    """Testing configuration with in-memory SQLite."""
    TESTING = True
    DEBUG = True
    
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False  # Disable CSRF for testing
    SLA_CHECK_INTERVAL_SECONDS = 0  # Always run in tests for determinism
    
    # Use temporary upload folder for tests
    UPLOAD_FOLDER = '/tmp/mibsp_test_uploads'
    
    @classmethod
    def init_app(cls, app):
        super().init_app(app)


class ProductionConfig(Config):
    """
    Production configuration.
    Supports both MySQL (VPS) and PostgreSQL (Render) transparently.
    """
    DEBUG = False
    TESTING = False
    
    # Enhanced security for production
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    
    @classmethod
    def init_app(cls, app):
        """Initialize with database URI detection."""
        super().init_app(app)
        
        uri = os.environ.get('DATABASE_URL', '')
        
        # Render gives postgres://, SQLAlchemy needs postgresql://
        if uri.startswith('postgres://'):
            uri = uri.replace('postgres://', 'postgresql://', 1)
        
        # VPS MySQL — build URI from parts
        if not uri and os.environ.get('MYSQL_HOST'):
            mysql_host = os.environ.get('MYSQL_HOST', 'localhost')
            mysql_user = os.environ.get('MYSQL_USER', 'mibsp_user')
            mysql_password = os.environ.get('MYSQL_PASSWORD', '')
            mysql_db = os.environ.get('MYSQL_DB', 'mibsp')
            
            uri = (
                f"mysql+pymysql://{mysql_user}"
                f":{mysql_password}"
                f"@{mysql_host}"
                f"/{mysql_db}?charset=utf8mb4"
            )
        
        # Fallback: SQLite (not recommended for production)
        if not uri:
            uri = 'sqlite:///' + os.path.join(basedir, 'instance', 'mibsp.db')
            app.logger.warning(
                'WARNING: Using SQLite in production is not recommended. '
                'Please configure MySQL or PostgreSQL.'
            )
        
        app.config['SQLALCHEMY_DATABASE_URI'] = uri
        
        # Log configuration (without sensitive data)
        db_type = 'PostgreSQL' if 'postgresql' in uri else ('MySQL' if 'mysql' in uri else 'SQLite')
        app.logger.info(f'Production database: {db_type}')


# Configuration dictionary for easy access
config = {
    'development': DevelopmentConfig,
    'testing': TestingConfig,
    'production': ProductionConfig,
    'default': DevelopmentConfig
}
