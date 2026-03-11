"""
MIBSP Authentication Routes
Login, logout, and session management.
"""
import secrets
import string
import time
import threading
from collections import deque
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse
from flask import Blueprint, render_template, request, flash, redirect, url_for, session, current_app
from werkzeug.security import generate_password_hash, check_password_hash

from app import db
from app.models import User
from app.utils import log_action, login_required
from app.tasks import send_system_email

auth_bp = Blueprint('auth', __name__)
_login_rate_lock = threading.Lock()
_login_rate_buckets = {}


def _resolve_next_target():
    """Read postback-safe redirect target from query string or form body."""
    return (request.form.get('next') or request.args.get('next') or '').strip()


def _is_safe_redirect(target):
    """Allow redirects only to same-host URLs."""
    if not target:
        return False
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return (
        test_url.scheme in {'http', 'https'}
        and ref_url.netloc == test_url.netloc
    )


def _set_authenticated_session(user):
    """Store authenticated user in session."""
    session.permanent = True
    session['user_id'] = user.id
    session['username'] = user.username
    session['role'] = user.role
    session['department_id'] = user.department_id


def _clear_pending_otp():
    """Clear temporary OTP challenge values from session."""
    session.pop('pending_otp_user_id', None)
    session.pop('pending_otp_hash', None)
    session.pop('pending_otp_expires_at', None)
    session.pop('pending_otp_next', None)


def _get_client_ip():
    """Resolve originating IP for login rate limiting."""
    forwarded = request.headers.get('X-Forwarded-For', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.remote_addr or 'unknown'


def _enforce_login_rate_limit():
    """Apply lightweight per-IP login throttling."""
    if not current_app.config.get('LOGIN_RATE_LIMIT_ENABLED', True):
        return True, None

    min_interval = int(current_app.config.get('LOGIN_RATE_MIN_INTERVAL_SECONDS', 1))
    window_seconds = int(current_app.config.get('LOGIN_RATE_WINDOW_SECONDS', 300))
    max_requests = int(current_app.config.get('LOGIN_RATE_MAX_ATTEMPTS_PER_IP', 25))
    client_ip = _get_client_ip()
    now_ts = time.time()

    with _login_rate_lock:
        bucket = _login_rate_buckets.get(client_ip)
        if bucket is None:
            bucket = {'last_ts': 0.0, 'hits': deque()}
            _login_rate_buckets[client_ip] = bucket

        if now_ts - bucket['last_ts'] < min_interval:
            return False, 'Please wait a moment before trying to log in again.'

        hits = bucket['hits']
        while hits and now_ts - hits[0] > window_seconds:
            hits.popleft()

        if len(hits) >= max_requests:
            return False, 'Too many login attempts from this network. Please try again later.'

        hits.append(now_ts)
        bucket['last_ts'] = now_ts

        if len(_login_rate_buckets) > 5000:
            stale_cutoff = now_ts - (window_seconds * 2)
            stale = [
                ip for ip, data in _login_rate_buckets.items()
                if not data['hits'] or data['hits'][-1] < stale_cutoff
            ]
            for ip in stale[:1000]:
                _login_rate_buckets.pop(ip, None)

    return True, None


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    """
    User login page.
    Only for officers and admins - citizens don't need accounts.
    """
    # Redirect if already logged in
    if 'user_id' in session:
        if session.get('role') == 'admin':
            return redirect(url_for('admin.dashboard'))
        else:
            return redirect(url_for('officer.dashboard'))
    
    next_page = _resolve_next_target()

    if request.method == 'POST':
        allowed, rate_message = _enforce_login_rate_limit()
        if not allowed:
            flash(rate_message, 'danger')
            log_action('LOGIN_RATE_LIMITED', details={'username': request.form.get('username', '').strip()})
            return render_template('auth/login.html', next=next_page)

        _clear_pending_otp()
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        if not username or not password:
            flash('Please enter both username and password.', 'warning')
            return render_template('auth/login.html', next=next_page)
        
        # Find user
        user = User.query.filter_by(username=username).first()

        if user and user.is_locked():
            flash('Account temporarily locked due to repeated failed attempts. Please try again later.', 'danger')
            log_action(
                'LOGIN_BLOCKED_LOCKED',
                details={'username': username}
            )
            return render_template('auth/login.html', next=next_page)
        
        if user and user.check_password(password):
            if not user.is_active:
                flash('Your account has been deactivated. Contact an administrator.', 'danger')
                log_action('LOGIN_FAILED_INACTIVE', 
                          details={'username': username}, user=user)
                return render_template('auth/login.html', next=next_page)

            if user.is_admin() and current_app.config.get('ADMIN_EMAIL_2FA_ENABLED', False):
                if not user.email:
                    flash('Admin account email is required for OTP verification.', 'danger')
                    return render_template('auth/login.html', next=next_page)

                otp_length = int(current_app.config.get('ADMIN_OTP_LENGTH', 6))
                otp_expiry_minutes = int(current_app.config.get('ADMIN_OTP_EXPIRY_MINUTES', 5))
                otp_code = ''.join(secrets.choice(string.digits) for _ in range(otp_length))
                otp_expires_at = datetime.utcnow() + timedelta(minutes=otp_expiry_minutes)

                subject = 'MIBSP Admin Login OTP'
                body = (
                    f'Your OTP for MIBSP admin login is: {otp_code}\n'
                    f'This code expires in {otp_expiry_minutes} minutes.'
                )
                sent, send_error = send_system_email(subject, body, [user.email])
                if not sent:
                    log_action('LOGIN_2FA_MAIL_FAILED', details={
                        'username': username,
                        'error': send_error or 'unknown'
                    }, user=user)
                    flash(
                        'Unable to send OTP email right now. '
                        'Verify MAIL settings and retry, or ask an admin to disable ADMIN_EMAIL_2FA_ENABLED.',
                        'danger'
                    )
                    return render_template('auth/login.html', next=next_page)

                _clear_pending_otp()
                session['pending_otp_user_id'] = user.id
                session['pending_otp_hash'] = generate_password_hash(
                    otp_code, method='pbkdf2:sha256', salt_length=8
                )
                session['pending_otp_expires_at'] = int(otp_expires_at.timestamp())
                session['pending_otp_next'] = next_page if _is_safe_redirect(next_page) else ''

                log_action('LOGIN_2FA_CHALLENGE_ISSUED', details={'username': username})
                flash('OTP sent to your admin email. Please verify to continue.', 'info')
                return redirect(url_for('auth.verify_admin_otp'))

            _set_authenticated_session(user)
            user.update_last_login()
            log_action('LOGIN_SUCCESS', user=user)
            flash(f'Welcome back, {user.username}!', 'success')

            if _is_safe_redirect(next_page):
                return redirect(next_page)

            if user.is_admin():
                return redirect(url_for('admin.dashboard'))
            return redirect(url_for('officer.dashboard'))
        
        else:
            if user:
                user.register_failed_login()
                db.session.commit()
                reason = 'invalid_credentials'
                if user.is_locked():
                    reason = 'account_locked'
            else:
                reason = 'invalid_credentials'

            # Log failed login
            log_action('LOGIN_FAILED', 
                      details={'username': username, 'reason': reason})

            if reason == 'account_locked':
                flash('Too many failed attempts. Account locked for 15 minutes.', 'danger')
            else:
                flash('Invalid username or password.', 'danger')

    return render_template('auth/login.html', next=next_page)


@auth_bp.route('/verify-otp', methods=['GET', 'POST'])
def verify_admin_otp():
    """Verify admin email OTP when 2FA is enabled."""
    pending_user_id = session.get('pending_otp_user_id')
    pending_hash = session.get('pending_otp_hash')
    pending_expiry = session.get('pending_otp_expires_at')

    if not pending_user_id or not pending_hash or not pending_expiry:
        flash('No pending OTP verification found. Please log in again.', 'warning')
        return redirect(url_for('auth.login'))

    if request.method == 'POST':
        otp = (request.form.get('otp') or '').strip()
        if not otp:
            flash('Please enter the OTP code.', 'danger')
            return render_template('auth/verify_otp.html')

        if int(datetime.utcnow().timestamp()) > int(pending_expiry):
            _clear_pending_otp()
            flash('OTP expired. Please log in again.', 'danger')
            return redirect(url_for('auth.login'))

        if not check_password_hash(pending_hash, otp):
            flash('Invalid OTP. Please try again.', 'danger')
            return render_template('auth/verify_otp.html')

        user = db.session.get(User, pending_user_id)
        if not user or not user.is_active or not user.is_admin():
            _clear_pending_otp()
            flash('Unable to verify user session. Please log in again.', 'danger')
            return redirect(url_for('auth.login'))

        next_page = session.get('pending_otp_next', '')
        _clear_pending_otp()

        _set_authenticated_session(user)
        user.update_last_login()
        log_action('LOGIN_SUCCESS', user=user)
        flash(f'Welcome back, {user.username}!', 'success')

        if _is_safe_redirect(next_page):
            return redirect(next_page)
        return redirect(url_for('admin.dashboard'))

    return render_template('auth/verify_otp.html')


@auth_bp.route('/logout', methods=['POST'])
def logout():
    """Logout and clear session."""
    if 'user_id' in session:
        # Log logout
        user = db.session.get(User, session['user_id'])
        if user:
            log_action('LOGOUT', user=user)
        
        # Clear session
        _clear_pending_otp()
        session.clear()
        flash('You have been logged out.', 'info')
    
    return redirect(url_for('public.index'))


@auth_bp.route('/profile')
@login_required
def profile():
    """User profile page."""
    user = db.session.get(User, session['user_id'])
    return render_template('auth/profile.html', user=user)


@auth_bp.route('/profile/change-password', methods=['POST'])
@login_required
def change_admin_password():
    """Allow admins to change their own password from profile."""
    if session.get('role') != 'admin':
        flash('Only admins can change password from this section.', 'danger')
        return redirect(url_for('auth.profile'))

    user = db.session.get(User, session['user_id'])
    if not user:
        flash('User not found.', 'danger')
        return redirect(url_for('auth.logout'))

    current_password = request.form.get('current_password', '')
    new_password = request.form.get('new_password', '')
    confirm_password = request.form.get('confirm_password', '')

    if not current_password or not new_password or not confirm_password:
        flash('All password fields are required.', 'danger')
        return redirect(url_for('auth.profile'))

    if not user.check_password(current_password):
        flash('Current password is incorrect.', 'danger')
        return redirect(url_for('auth.profile'))

    if len(new_password) < 8:
        flash('New password must be at least 8 characters.', 'danger')
        return redirect(url_for('auth.profile'))

    if new_password != confirm_password:
        flash('New password and confirmation do not match.', 'danger')
        return redirect(url_for('auth.profile'))

    if new_password == current_password:
        flash('New password must be different from current password.', 'danger')
        return redirect(url_for('auth.profile'))

    # Keep policy lightweight but safer than length-only passwords.
    if not any(ch.isalpha() for ch in new_password) or not any(ch.isdigit() for ch in new_password):
        flash('New password must include at least one letter and one number.', 'danger')
        return redirect(url_for('auth.profile'))

    try:
        user.set_password(new_password)
        db.session.commit()

        log_action('ADMIN_PASSWORD_CHANGED', user=user)
        flash('Admin password updated successfully.', 'success')
    except Exception:
        db.session.rollback()
        flash('Error updating password. Please try again.', 'danger')

    return redirect(url_for('auth.profile'))
