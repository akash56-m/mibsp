"""
MIBSP Task Utilities
Synchronous task helpers used when Celery/Redis are not configured.
"""
import logging
import smtplib
import urllib.parse
import urllib.request
from email.message import EmailMessage

from flask import current_app, has_app_context

from app.models import Complaint, User

logger = logging.getLogger(__name__)


def _collect_status_update_recipients(complaint):
    """Collect recipient emails for staff notifications."""
    recipients = set()

    if complaint and complaint.assigned_officer and complaint.assigned_officer.email:
        recipients.add(complaint.assigned_officer.email.strip())

    admins = User.query.filter_by(role='admin', is_active=True).all()
    for admin in admins:
        if admin.email:
            recipients.add(admin.email.strip())

    fallback = (current_app.config.get('NOTIFICATION_TO_EMAIL') or '').strip()
    if fallback:
        recipients.add(fallback)

    return sorted(email for email in recipients if email)


def _collect_submission_recipients():
    """Collect recipient emails for new complaint alerts."""
    recipients = set()

    admins = User.query.filter_by(role='admin', is_active=True).all()
    for admin in admins:
        if admin.email:
            recipients.add(admin.email.strip())

    officers = User.query.filter(
        User.role.in_(['officer', 'zonal_officer', 'commissioner']),
        User.is_active.is_(True)
    ).all()
    for officer in officers:
        if officer.email:
            recipients.add(officer.email.strip())

    fallback = (current_app.config.get('NOTIFICATION_TO_EMAIL') or '').strip()
    if fallback:
        recipients.add(fallback)

    return sorted(email for email in recipients if email)


def _collect_sms_recipients():
    """Collect SMS recipients from config."""
    raw = (current_app.config.get('SMS_NOTIFICATION_TO') or '').strip()
    if not raw:
        return []
    recipients = []
    for part in raw.split(','):
        number = part.strip()
        if number:
            recipients.append(number)
    return recipients


def send_system_email(subject, body, recipients):
    """Send SMTP email if mail settings are configured."""
    mail_server = (current_app.config.get('MAIL_SERVER') or '').strip()
    mail_port = int(current_app.config.get('MAIL_PORT', 587))
    mail_use_tls = bool(current_app.config.get('MAIL_USE_TLS', True))
    mail_username = (current_app.config.get('MAIL_USERNAME') or '').strip()
    mail_password = current_app.config.get('MAIL_PASSWORD') or ''

    if not mail_server:
        return False, 'MAIL_SERVER not configured.'
    if not recipients:
        return False, 'No recipients available.'

    sender = mail_username or 'no-reply@mibsp.local'

    message = EmailMessage()
    message['Subject'] = subject
    message['From'] = sender
    message['To'] = ', '.join(recipients)
    message.set_content(body)

    try:
        with smtplib.SMTP(mail_server, mail_port, timeout=15) as smtp:
            smtp.ehlo()
            if mail_use_tls:
                smtp.starttls()
                smtp.ehlo()
            if mail_username and mail_password:
                smtp.login(mail_username, mail_password)
            smtp.send_message(message)
        return True, None
    except Exception as exc:
        logger.exception('Email notification failed.')
        return False, str(exc)


def send_system_sms(message, recipients):
    """Send SMS messages via Twilio REST when configured."""
    if not current_app.config.get('SMS_ENABLED', False):
        return False, 'SMS is disabled.'
    if current_app.config.get('SMS_PROVIDER', 'twilio') != 'twilio':
        return False, 'Unsupported SMS provider configured.'
    if not recipients:
        return False, 'No SMS recipients available.'

    account_sid = (current_app.config.get('TWILIO_ACCOUNT_SID') or '').strip()
    auth_token = (current_app.config.get('TWILIO_AUTH_TOKEN') or '').strip()
    from_number = (current_app.config.get('TWILIO_FROM_NUMBER') or '').strip()
    if not account_sid or not auth_token or not from_number:
        return False, 'Twilio credentials are incomplete.'

    base_url = f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json'
    auth_handler = urllib.request.HTTPBasicAuthHandler()
    auth_handler.add_password(
        realm=None,
        uri=base_url,
        user=account_sid,
        passwd=auth_token
    )
    opener = urllib.request.build_opener(auth_handler)
    urllib.request.install_opener(opener)

    sent_count = 0
    errors = []
    for to_number in recipients:
        payload = urllib.parse.urlencode({
            'To': to_number,
            'From': from_number,
            'Body': message
        }).encode('utf-8')
        request = urllib.request.Request(base_url, data=payload, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=15):
                sent_count += 1
        except Exception as exc:
            logger.exception('SMS notification failed for %s.', to_number)
            errors.append(str(exc))

    if sent_count > 0:
        return True, None
    return False, '; '.join(errors[:3]) if errors else 'SMS send failed.'


def send_status_update_notification(tracking_id, new_status, contact_method=None):
    """
    Send status-update notifications to staff emails when configured.
    Falls back to structured logging if email settings are unavailable.
    """
    if not has_app_context():
        logger.info(
            '[TASK] Notification skipped (no app context): complaint=%s status=%s',
            tracking_id, new_status
        )
        return {
            'success': False,
            'tracking_id': tracking_id,
            'status': new_status,
            'mode': 'skipped'
        }

    complaint = Complaint.query.filter_by(tracking_id=tracking_id).first()
    recipients = _collect_status_update_recipients(complaint)

    subject = f'MIBSP Update: Complaint {tracking_id} is now {new_status}'
    body = (
        f'Complaint Tracking ID: {tracking_id}\n'
        f'New Status: {new_status}\n'
        f'Department: {complaint.department.name if complaint and complaint.department else "N/A"}\n'
        f'Service: {complaint.service.name if complaint and complaint.service else "N/A"}\n'
    )

    sent, error = send_system_email(subject, body, recipients)
    sms_recipients = _collect_sms_recipients()
    sms_sent = False
    sms_error = None
    if sms_recipients:
        sms_message = (
            f'MIBSP Alert: {tracking_id} status changed to {new_status}. '
            f'Department: {complaint.department.name if complaint and complaint.department else "N/A"}'
        )
        sms_sent, sms_error = send_system_sms(sms_message, sms_recipients)

    if sent:
        logger.info(
            '[TASK] Email notification sent: complaint=%s status=%s recipients=%s',
            tracking_id, new_status, len(recipients)
        )
        return {
            'success': True,
            'tracking_id': tracking_id,
            'status': new_status,
            'mode': 'email',
            'recipient_count': len(recipients),
            'sms_sent': sms_sent
        }

    logger.info(
        '[TASK] Notification fallback: complaint=%s status=%s reason=%s',
        tracking_id, new_status, error
    )
    return {
        'success': True,
        'tracking_id': tracking_id,
        'status': new_status,
        'mode': 'log',
        'reason': error,
        'sms_sent': sms_sent,
        'sms_reason': sms_error
    }


def send_complaint_submission_notification(tracking_id):
    """
    Send new complaint submission notifications to internal staff channels.
    Uses email by default and optional SMS when configured.
    """
    if not has_app_context():
        logger.info('[TASK] Submission notification skipped (no app context): complaint=%s', tracking_id)
        return {'success': False, 'tracking_id': tracking_id, 'mode': 'skipped'}

    complaint = Complaint.query.filter_by(tracking_id=tracking_id).first()
    if not complaint:
        return {'success': False, 'tracking_id': tracking_id, 'mode': 'missing_complaint'}

    recipients = _collect_submission_recipients()
    subject = f'MIBSP New Complaint Submitted: {tracking_id}'
    body = (
        f'Complaint Tracking ID: {tracking_id}\n'
        f'Department: {complaint.department.name if complaint.department else "N/A"}\n'
        f'Service: {complaint.service.name if complaint.service else "N/A"}\n'
        f'Priority: {complaint.priority}\n'
        f'Status: {complaint.status}\n'
        f'Submitted At: {complaint.submitted_at.isoformat() if complaint.submitted_at else "N/A"}\n'
    )
    email_sent, email_error = send_system_email(subject, body, recipients)

    sms_recipients = _collect_sms_recipients()
    sms_sent = False
    sms_error = None
    if sms_recipients:
        sms_message = (
            f'New MIBSP complaint {tracking_id} '
            f'({complaint.priority}/{complaint.status}) in '
            f'{complaint.department.name if complaint.department else "N/A"}.'
        )
        sms_sent, sms_error = send_system_sms(sms_message, sms_recipients)

    if email_sent or sms_sent:
        return {
            'success': True,
            'tracking_id': tracking_id,
            'email_sent': email_sent,
            'sms_sent': sms_sent,
            'recipient_count': len(recipients),
            'sms_recipient_count': len(sms_recipients)
        }

    logger.info(
        '[TASK] Submission notification fallback: complaint=%s email_reason=%s sms_reason=%s',
        tracking_id,
        email_error,
        sms_error
    )
    return {
        'success': True,
        'tracking_id': tracking_id,
        'mode': 'log',
        'email_reason': email_error,
        'sms_reason': sms_error
    }


def generate_daily_report():
    """Placeholder daily report hook for scheduler integration."""
    logger.info('[TASK] Daily report generation is not scheduled in this runtime.')
    return {}


def cleanup_old_uploads(days=30):
    """Placeholder upload cleanup hook for scheduler integration."""
    logger.info('[TASK] Upload cleanup is not scheduled in this runtime.')
    return {}


def backup_database():
    """Placeholder database backup hook for scheduler integration."""
    logger.info('[TASK] Database backup is not scheduled in this runtime.')
    return {}
