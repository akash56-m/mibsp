"""
MIBSP Public Routes
Citizen-facing routes - no authentication required.
"""
import csv
import io
import time
import threading
from collections import deque
from flask import Blueprint, render_template, request, flash, redirect, url_for, jsonify, current_app, Response
from sqlalchemy import text
from sqlalchemy.orm import joinedload
from datetime import datetime, timedelta

from app import db
from app.models import Department, Service, Complaint, AuditLog
from app.utils import (
    generate_tracking_id, save_uploaded_file, 
    validate_tracking_id, log_action, analyze_complaint_text, maybe_run_sla_escalations
)

public_bp = Blueprint('public', __name__)
_ai_rate_lock = threading.Lock()
_ai_rate_buckets = {}

DASHBOARD_STATUSES = ['Pending', 'Under Review', 'Action Taken', 'Delayed', 'Reopened', 'Closed']
STATUS_BADGE_CLASSES = {
    'Pending': 'badge-pending',
    'Under Review': 'badge-review',
    'Action Taken': 'badge-action',
    'Delayed': 'badge-delayed',
    'Reopened': 'badge-reopened',
    'Closed': 'badge-closed',
}


def _get_client_ip():
    forwarded = request.headers.get('X-Forwarded-For', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.remote_addr or 'unknown'


def _enforce_ai_rate_limit():
    """
    Apply in-memory per-IP limits:
    - minimum seconds between requests
    - maximum requests in a rolling window
    """
    min_interval = int(current_app.config.get('AI_RATE_MIN_INTERVAL_SECONDS', 3))
    window_seconds = int(current_app.config.get('AI_RATE_WINDOW_SECONDS', 60))
    max_requests = int(current_app.config.get('AI_RATE_MAX_REQUESTS_PER_WINDOW', 20))

    client_ip = _get_client_ip()
    now_ts = time.time()

    with _ai_rate_lock:
        bucket = _ai_rate_buckets.get(client_ip)
        if bucket is None:
            bucket = {'last_ts': 0.0, 'hits': deque()}
            _ai_rate_buckets[client_ip] = bucket

        if now_ts - bucket['last_ts'] < min_interval:
            return False, 'Please wait a few seconds before asking again.'

        hits = bucket['hits']
        while hits and now_ts - hits[0] > window_seconds:
            hits.popleft()

        if len(hits) >= max_requests:
            return False, 'Too many AI requests from this network. Please try again later.'

        hits.append(now_ts)
        bucket['last_ts'] = now_ts

        # Prevent indefinite growth if many unique IPs touch the endpoint.
        if len(_ai_rate_buckets) > 5000:
            stale_cutoff = now_ts - (window_seconds * 2)
            stale_keys = [
                ip for ip, data in _ai_rate_buckets.items()
                if not data['hits'] or data['hits'][-1] < stale_cutoff
            ]
            for ip in stale_keys[:1000]:
                _ai_rate_buckets.pop(ip, None)

    return True, None


def _fallback_homepage_reply(message):
    """Provide deterministic portal guidance when external AI is unavailable."""
    text = (message or '').lower()

    if any(word in text for word in ['submit', 'report', 'complaint']):
        return (
            "To submit a strong anonymous complaint, include what happened, where it happened, "
            "when it happened, and what impact it caused. Keep facts specific and avoid personal identifiers.\n"
            "Next best action: Open /submit and complete the complaint form with clear details."
        )

    if any(word in text for word in ['track', 'status', 'id']):
        return (
            "You can track progress using your complaint tracking ID (starts with MIB). "
            "Enter it on the tracking page to see current status and timeline updates.\n"
            "Next best action: Open /track and search with your tracking ID."
        )

    if any(word in text for word in ['evidence', 'proof', 'photo', 'file', 'document']):
        return (
            "Useful evidence includes photos, documents, receipts, and timestamps connected to the incident. "
            "Upload only relevant files and avoid personal identifiers in attachments.\n"
            "Next best action: Prepare supporting files, then submit through /submit."
        )

    if any(word in text for word in ['dashboard', 'stats', 'heatmap', 'transparency']):
        return (
            "The public dashboard shows aggregate complaint performance, while the geo heatmap shows location trends. "
            "These tools help you understand resolution patterns.\n"
            "Next best action: Visit /dashboard for analytics and /geo-heatmap for map insights."
        )

    return (
        "I can help with complaint submission, status tracking, evidence guidance, and transparency pages. "
        "Ask a specific question to get step-by-step guidance.\n"
        "Next best action: Tell me whether you want help with /submit, /track, /dashboard, or /geo-heatmap."
    )


def _fallback_draft_reply(message, description, department_name, service_name):
    """Provide structured drafting help without external AI."""
    draft = (description or '').strip()
    brief_draft = draft[:300] if draft else (
        "I want to report misconduct related to municipal service delivery in my area."
    )

    return (
        "1) Quick guidance\n"
        "Keep your complaint factual and specific. Mention incident date/time, location, requested action, and impact.\n\n"
        "2) Improved complaint draft (template)\n"
        f"I am submitting an anonymous complaint regarding {service_name or 'a municipal service'}"
        f" under {department_name or 'the relevant department'}. "
        "The incident occurred at [location] on [date/time]. The issue involved [clear factual description]. "
        "This caused [impact on citizens/service delivery]. Any available evidence includes [documents/photos/reference details]. "
        "I request a formal review and corrective action, and I request updates against the complaint tracking ID.\n\n"
        f"Context from your draft: {brief_draft}\n\n"
        "3) Missing details checklist\n"
        "- Exact location and approximate time\n"
        "- Specific action/behavior observed\n"
        "- Service impact and frequency\n"
        "- Evidence references (if available)"
    )


def _fallback_ai_reply(assistant_mode, message, description, department_name, service_name):
    """Return local fallback text for chatbot responses."""
    if assistant_mode == 'homepage':
        return _fallback_homepage_reply(message)
    return _fallback_draft_reply(message, description, department_name, service_name)


def _month_start(value):
    """Normalize datetime to month start."""
    return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _shift_month(value, months):
    """Shift datetime by N months, preserving month-start format."""
    month_index = (value.month - 1) + months
    year = value.year + (month_index // 12)
    month = (month_index % 12) + 1
    return value.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)


def _parse_month_value(raw):
    """Parse YYYY-MM into datetime at month start."""
    if not raw:
        return None
    try:
        parsed = datetime.strptime(raw, '%Y-%m')
        return _month_start(parsed)
    except ValueError:
        return None


def _iter_month_starts(from_month_start, to_month_start):
    """Yield month starts in inclusive range."""
    if from_month_start is None or to_month_start is None:
        return []
    months = []
    cursor = from_month_start
    while cursor <= to_month_start:
        months.append(cursor)
        cursor = _shift_month(cursor, 1)
    return months


def _parse_dashboard_filters(default_month_window=False):
    """
    Parse dashboard filters from query params.
    Supports:
    - department_id (int)
    - status (enum)
    - from_month, to_month (YYYY-MM)
    """
    department_id = request.args.get('department_id', type=int)
    if department_id is not None and department_id <= 0:
        department_id = None

    status = (request.args.get('status') or '').strip()
    if status and status not in DASHBOARD_STATUSES:
        raise ValueError('Invalid status filter.')

    from_month_raw = (request.args.get('from_month') or '').strip()
    to_month_raw = (request.args.get('to_month') or '').strip()

    from_month_start = _parse_month_value(from_month_raw) if from_month_raw else None
    to_month_start = _parse_month_value(to_month_raw) if to_month_raw else None

    if from_month_raw and from_month_start is None:
        raise ValueError('Invalid from_month format. Use YYYY-MM.')
    if to_month_raw and to_month_start is None:
        raise ValueError('Invalid to_month format. Use YYYY-MM.')

    if from_month_start and not to_month_start:
        to_month_start = from_month_start
    if to_month_start and not from_month_start:
        from_month_start = to_month_start

    if from_month_start is None and to_month_start is None and default_month_window:
        to_month_start = _month_start(datetime.utcnow())
        from_month_start = _shift_month(to_month_start, -11)

    if from_month_start and to_month_start and from_month_start > to_month_start:
        raise ValueError('from_month must be earlier than or equal to to_month.')

    to_month_end = _shift_month(to_month_start, 1) if to_month_start else None

    return {
        'department_id': department_id,
        'status': status or None,
        'from_month_start': from_month_start,
        'to_month_start': to_month_start,
        'to_month_end': to_month_end,
        'from_month': from_month_start.strftime('%Y-%m') if from_month_start else '',
        'to_month': to_month_start.strftime('%Y-%m') if to_month_start else '',
    }


def _apply_dashboard_filters(query, filters, date_field=Complaint.submitted_at, include_time_window=True):
    """Apply reusable dashboard filters to a complaint query."""
    if filters.get('department_id'):
        query = query.filter(Complaint.department_id == filters['department_id'])

    if filters.get('status'):
        query = query.filter(Complaint.status == filters['status'])

    if include_time_window and filters.get('from_month_start') and filters.get('to_month_end'):
        query = query.filter(
            date_field >= filters['from_month_start'],
            date_field < filters['to_month_end']
        )

    return query


def _compute_dashboard_stats(filters):
    """Compute aggregate dashboard stats for current filter set."""
    base_query = _apply_dashboard_filters(Complaint.query, filters, include_time_window=True)

    total = base_query.count()
    pending = base_query.filter(Complaint.status == 'Pending').count()
    under_review = base_query.filter(Complaint.status == 'Under Review').count()
    action_taken = base_query.filter(Complaint.status == 'Action Taken').count()
    delayed = base_query.filter(Complaint.status == 'Delayed').count()
    reopened = base_query.filter(Complaint.status == 'Reopened').count()
    closed = base_query.filter(Complaint.status == 'Closed').count()
    high_priority = base_query.filter(Complaint.priority == 'High').count()

    closed_items = base_query.filter(Complaint.status == 'Closed').all()
    within_sla = sum(
        1 for item in closed_items
        if item.sla_due_at and item.resolved_at and item.resolved_at <= item.sla_due_at
    )
    sla_compliance = round((within_sla / len(closed_items) * 100), 2) if closed_items else 0
    resolution_rate = round((closed / total * 100), 2) if total > 0 else 0
    in_progress = under_review + action_taken + delayed + reopened
    backlog_rate = round(((pending + in_progress) / total * 100), 2) if total > 0 else 0

    return {
        'total': total,
        'pending': pending,
        'under_review': under_review,
        'action_taken': action_taken,
        'delayed': delayed,
        'reopened': reopened,
        'closed': closed,
        'high_priority': high_priority,
        'sla_compliance': sla_compliance,
        'resolution_rate': resolution_rate,
        'in_progress': in_progress,
        'backlog_rate': backlog_rate,
    }


def _compute_department_stats(filters):
    """Compute per-department stats for scoreboard and ranking."""
    departments_query = Department.query.order_by(Department.name.asc())
    if filters.get('department_id'):
        departments_query = departments_query.filter(Department.id == filters['department_id'])
    departments = departments_query.all()

    dept_stats = []
    for dept in departments:
        dept_query = _apply_dashboard_filters(
            Complaint.query.filter(Complaint.department_id == dept.id),
            filters,
            include_time_window=True
        )
        total = dept_query.count()
        pending = dept_query.filter(Complaint.status == 'Pending').count()
        closed = dept_query.filter(Complaint.status == 'Closed').count()
        delayed = dept_query.filter(Complaint.status == 'Delayed').count()
        resolution_rate = round((closed / total * 100), 1) if total > 0 else 0
        delay_penalty = round((delayed / total * 100) * 0.5, 1) if total > 0 else 0
        transparency_score = round(max(resolution_rate - delay_penalty, 0), 1)

        dept_stats.append({
            'id': dept.id,
            'name': dept.name,
            'total': total,
            'pending': pending,
            'closed': closed,
            'delayed': delayed,
            'resolution_rate': resolution_rate,
            'delay_penalty': delay_penalty,
            'score': transparency_score
        })

    ranked = sorted(
        [item for item in dept_stats if item['total'] > 0],
        key=lambda item: item['score'],
        reverse=True
    )
    best_department = ranked[0] if ranked else None
    worst_department = ranked[-1] if ranked else None

    return dept_stats, best_department, worst_department


# =============================================================================
# HOMEPAGE & STATIC PAGES
# =============================================================================

@public_bp.route('/')
def index():
    """Homepage with hero section and quick stats."""
    maybe_run_sla_escalations()
    stats = Complaint.get_stats()
    departments = Department.query.all()
    
    return render_template('public/index.html', 
                          stats=stats, 
                          departments=departments)


@public_bp.route('/about')
def about():
    """About page explaining the portal."""
    return render_template('public/about.html')


@public_bp.route('/how-it-works')
def how_it_works():
    """How it works page with process explanation."""
    return render_template('public/how_it_works.html')


@public_bp.route('/favicon.ico')
def favicon():
    """Serve site favicon through static asset pipeline."""
    return redirect(url_for('static', filename='favicon.svg'))


@public_bp.route('/geo-heatmap')
def geo_heatmap():
    """Public geospatial complaint heatmap visualization."""
    maybe_run_sla_escalations()
    stats = Complaint.get_stats()
    return render_template('public/geo_heatmap.html', stats=stats)


# =============================================================================
# COMPLAINT SUBMISSION
# =============================================================================

@public_bp.route('/submit', methods=['GET', 'POST'])
def submit_complaint():
    """
    Anonymous complaint submission form.
    No login required, no PII collected.
    """
    if request.method == 'POST':
        # Get form data
        department_id = request.form.get('department_id', type=int)
        service_id = request.form.get('service_id', type=int)
        description = request.form.get('description', '').strip()
        location_lat = request.form.get('location_lat', type=float)
        location_lng = request.form.get('location_lng', type=float)
        state = (request.form.get('state') or '').strip() or None
        district = (request.form.get('district') or '').strip() or None
        city = (request.form.get('city') or '').strip() or None
        
        # Server-side validation
        errors = []
        service = None
        
        if not department_id:
            errors.append('Please select a department.')
        if not service_id:
            errors.append('Please select a service.')
        if not description or len(description) < 50:
            errors.append('Description must be at least 50 characters.')
        if len(description) > 5000:
            errors.append('Description must not exceed 5000 characters.')
        
        # Validate department and service relationship
        if department_id and service_id:
            service = db.session.get(Service, service_id)
            if not service or service.department_id != department_id:
                errors.append('Invalid service selection for this department.')

        # Optional geo validation
        if location_lat is not None and not (-90 <= location_lat <= 90):
            errors.append('Latitude must be between -90 and 90.')
        if location_lng is not None and not (-180 <= location_lng <= 180):
            errors.append('Longitude must be between -180 and 180.')
        if state and len(state) > 80:
            errors.append('State must be 80 characters or fewer.')
        if district and len(district) > 120:
            errors.append('District must be 120 characters or fewer.')
        if city and len(city) > 120:
            errors.append('City must be 120 characters or fewer.')
        
        if errors:
            for error in errors:
                flash(error, 'danger')
            departments = Department.query.all()
            return render_template('public/submit.html', 
                                  departments=departments,
                                  form_data=request.form)
        
        # Handle file upload
        evidence_path = None
        if 'evidence' in request.files:
            file = request.files['evidence']
            if file and file.filename:
                success, result = save_uploaded_file(file)
                if success:
                    evidence_path = result
                else:
                    flash(f'File upload error: {result}', 'warning')
        
        # Create complaint
        try:
            analysis = analyze_complaint_text(description)
            now = datetime.utcnow()
            complaint = Complaint(
                tracking_id=generate_tracking_id(),
                service_id=service_id,
                department_id=department_id,
                description=description,
                evidence_path=evidence_path,
                status='Pending',
                submitted_at=now,
                updated_at=now,
                priority=analysis['priority'],
                ai_category=analysis['category'],
                ai_sentiment=analysis['sentiment'],
                ai_urgent=analysis['urgent'],
                state=state,
                district=district,
                city=city,
                location_lat=location_lat,
                location_lng=location_lng
            )
            complaint.initialize_sla_due()

            # Urgent complaints are prioritized and escalated immediately.
            if complaint.ai_urgent:
                complaint.status = 'Under Review'
                complaint.escalation_level = 1
                complaint.assign_by_escalation_hierarchy()
            
            db.session.add(complaint)
            db.session.commit()
            
            # Log the submission (anonymous - no user)
            log_action('COMPLAINT_SUBMITTED', 
                      details={
                          'tracking_id': complaint.tracking_id,
                          'priority': complaint.priority,
                          'ai_urgent': complaint.ai_urgent,
                          'ai_category': complaint.ai_category
                      })
            
            flash('Complaint submitted successfully!', 'success')
            return redirect(url_for('public.confirmation', 
                                   tracking_id=complaint.tracking_id))
            
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f'Complaint submission error: {str(e)}')
            flash('Error submitting complaint. Please try again.', 'danger')
            departments = Department.query.all()
            return render_template('public/submit.html', 
                                  departments=departments,
                                  form_data=request.form)
    
    # GET request - show form
    departments = Department.query.all()
    return render_template('public/submit.html', departments=departments)


@public_bp.route('/confirmation/<tracking_id>')
def confirmation(tracking_id):
    """Confirmation page showing tracking ID."""
    complaint = Complaint.query.filter_by(tracking_id=tracking_id).first_or_404()
    return render_template('public/confirm.html', complaint=complaint)


# =============================================================================
# COMPLAINT TRACKING
# =============================================================================

@public_bp.route('/track', methods=['GET', 'POST'])
def track_complaint():
    """
    Public complaint tracking - no login required.
    Only shows non-sensitive information.
    """
    maybe_run_sla_escalations()
    complaint = None
    tracking_id = request.args.get('tracking_id', '').strip().upper()

    if request.method == 'POST':
        tracking_id = request.form.get('tracking_id', '').strip().upper()

    if tracking_id:
        if not validate_tracking_id(tracking_id):
            flash('Invalid tracking ID format.', 'danger')
        else:
            complaint = Complaint.query.filter_by(tracking_id=tracking_id).first()
            if not complaint:
                flash('Complaint not found. Please check your tracking ID.', 'warning')
            else:
                # Log tracking access (anonymous)
                log_action('COMPLAINT_TRACKED', details={'tracking_id': tracking_id})
    elif request.method == 'POST':
        flash('Please enter a tracking ID.', 'warning')
    
    return render_template('public/track.html', 
                          complaint=complaint, 
                          tracking_id=tracking_id)


@public_bp.route('/complaint/<tracking_id>/reopen', methods=['POST'])
def reopen_complaint(tracking_id):
    """Allow citizen to reopen a closed complaint with reason."""
    complaint = Complaint.query.filter_by(tracking_id=tracking_id).first_or_404()
    reason = request.form.get('reopen_reason', '').strip()

    success, message = complaint.reopen(reason)
    if not success:
        flash(message, 'danger')
        return redirect(url_for('public.track_complaint', tracking_id=tracking_id))

    db.session.commit()
    log_action('COMPLAINT_REOPENED_BY_CITIZEN', details={
        'tracking_id': tracking_id,
        'reopen_count': complaint.reopen_count
    })
    flash(message, 'success')
    return redirect(url_for('public.track_complaint', tracking_id=tracking_id))


@public_bp.route('/complaint/<tracking_id>/feedback', methods=['POST'])
def submit_feedback(tracking_id):
    """Allow anonymous rating/feedback after complaint closure."""
    complaint = Complaint.query.filter_by(tracking_id=tracking_id).first_or_404()
    rating = request.form.get('rating', type=int)
    feedback = request.form.get('feedback', '').strip()

    if feedback and len(feedback) > 1000:
        flash('Feedback must be under 1000 characters.', 'danger')
        return redirect(url_for('public.track_complaint', tracking_id=tracking_id))

    success, message = complaint.submit_citizen_feedback(rating or 0, feedback)
    if not success:
        flash(message, 'danger')
        return redirect(url_for('public.track_complaint', tracking_id=tracking_id))

    db.session.commit()
    log_action('CITIZEN_FEEDBACK_SUBMITTED', details={
        'tracking_id': tracking_id,
        'rating': complaint.citizen_rating
    })
    flash(message, 'success')
    return redirect(url_for('public.track_complaint', tracking_id=tracking_id))


# =============================================================================
# PUBLIC DASHBOARD
# =============================================================================

@public_bp.route('/dashboard')
def public_dashboard():
    """
    Public analytics dashboard.
    Shows aggregate statistics only - no sensitive data.
    """
    maybe_run_sla_escalations()

    default_filters = {
        'department_id': None,
        'status': None,
        'from_month_start': None,
        'to_month_start': None,
        'to_month_end': None,
        'from_month': '',
        'to_month': '',
    }

    stats = _compute_dashboard_stats(default_filters)
    dept_stats, best_department, worst_department = _compute_department_stats(default_filters)
    
    # Recent activity (last 30 days)
    thirty_days_ago = datetime.utcnow() - timedelta(days=30)
    recent_complaints = Complaint.query.filter(
        Complaint.submitted_at >= thirty_days_ago
    ).order_by(Complaint.submitted_at.desc()).limit(10).all()
    
    return render_template('public/dashboard.html',
                          stats=stats,
                          dept_stats=dept_stats,
                          best_department=best_department,
                          worst_department=worst_department,
                          recent_complaints=recent_complaints,
                          status_options=DASHBOARD_STATUSES)


# =============================================================================
# API ENDPOINTS
# =============================================================================

@public_bp.route('/api/services/<int:department_id>')
def get_services(department_id):
    """
    AJAX endpoint to get services for a department.
    Used in complaint form for dynamic service dropdown.
    """
    services = Service.query.filter_by(department_id=department_id).all()
    return jsonify([service.to_dict() for service in services])


@public_bp.route('/api/stats')
def get_stats():
    """API endpoint for statistics (used by charts)."""
    maybe_run_sla_escalations()
    return jsonify(Complaint.get_stats())


@public_bp.route('/api/dashboard/overview')
def get_dashboard_overview():
    """Filtered dashboard payload for client-side interactive updates."""
    maybe_run_sla_escalations()

    try:
        filters = _parse_dashboard_filters(default_month_window=False)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    stats = _compute_dashboard_stats(filters)
    dept_stats, best_department, worst_department = _compute_department_stats(filters)
    ranked_departments = sorted(
        dept_stats,
        key=lambda item: (item['score'], item['total']),
        reverse=True
    )
    active_departments = len([item for item in dept_stats if item['total'] > 0])

    recent_query = _apply_dashboard_filters(
        Complaint.query.options(
            joinedload(Complaint.department),
            joinedload(Complaint.service)
        ),
        filters,
        include_time_window=True
    )
    recent_complaints = recent_query.order_by(Complaint.submitted_at.desc()).limit(10).all()
    recent_serialized = []
    for complaint in recent_complaints:
        recent_serialized.append({
            'tracking_id': complaint.tracking_id,
            'department': complaint.department.name if complaint.department else '',
            'service': complaint.service.name if complaint.service else '',
            'status': complaint.status,
            'status_badge': STATUS_BADGE_CLASSES.get(complaint.status, 'badge-secondary'),
            'submitted_at': (
                complaint.submitted_at.strftime('%d %b %Y, %I:%M %p')
                if complaint.submitted_at else 'N/A'
            )
        })

    return jsonify({
        'filters': {
            'department_id': filters.get('department_id'),
            'status': filters.get('status') or '',
            'from_month': filters.get('from_month') or '',
            'to_month': filters.get('to_month') or '',
        },
        'stats': stats,
        'active_departments': active_departments,
        'best_department': best_department,
        'worst_department': worst_department,
        'dept_stats': ranked_departments,
        'recent_complaints': recent_serialized,
    })


@public_bp.route('/api/chart/monthly')
def get_monthly_chart_data():
    """Get monthly complaint data for Chart.js."""
    try:
        filters = _parse_dashboard_filters(default_month_window=True)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    base_query = _apply_dashboard_filters(
        Complaint.query,
        filters,
        include_time_window=False
    )

    labels = []
    data = []
    for month_start in _iter_month_starts(filters['from_month_start'], filters['to_month_start']):
        month_end = _shift_month(month_start, 1)
        count = base_query.filter(
            Complaint.submitted_at >= month_start,
            Complaint.submitted_at < month_end
        ).count()
        labels.append(month_start.strftime('%b %Y'))
        data.append(count)

    return jsonify({'labels': labels, 'data': data})


@public_bp.route('/api/chart/dept')
def get_dept_chart_data():
    """Get department-wise complaint data for Chart.js."""
    try:
        filters = _parse_dashboard_filters(default_month_window=False)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    departments_query = Department.query.order_by(Department.name.asc())
    if filters.get('department_id'):
        departments_query = departments_query.filter(Department.id == filters['department_id'])
    departments = departments_query.all()

    labels = []
    data = []
    
    for dept in departments:
        count = _apply_dashboard_filters(
            Complaint.query.filter(Complaint.department_id == dept.id),
            filters,
            include_time_window=True
        ).count()
        labels.append(dept.name)
        data.append(count)
    
    return jsonify({
        'labels': labels,
        'data': data
    })


@public_bp.route('/api/chart/status')
def get_status_chart_data():
    """Get status breakdown for Chart.js doughnut chart."""
    try:
        filters = _parse_dashboard_filters(default_month_window=False)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    base_query = _apply_dashboard_filters(
        Complaint.query,
        filters,
        include_time_window=True
    )

    statuses = DASHBOARD_STATUSES
    data = []
    
    for status in statuses:
        count = base_query.filter(Complaint.status == status).count()
        data.append(count)
    
    return jsonify({
        'labels': statuses,
        'data': data
    })


@public_bp.route('/api/chart/resolution-time')
def get_resolution_time_chart_data():
    """Average resolution hours per month for closed complaints."""
    try:
        filters = _parse_dashboard_filters(default_month_window=True)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    base_query = _apply_dashboard_filters(
        Complaint.query.filter(Complaint.resolved_at.isnot(None)),
        filters,
        date_field=Complaint.resolved_at,
        include_time_window=False
    )

    labels = []
    values = []
    for month_start in _iter_month_starts(filters['from_month_start'], filters['to_month_start']):
        month_end = _shift_month(month_start, 1)
        closed = base_query.filter(
            Complaint.resolved_at >= month_start,
            Complaint.resolved_at < month_end
        ).all()
        avg_hours = round(
            sum(c.get_resolution_time() or 0 for c in closed) / len(closed),
            2
        ) if closed else 0
        labels.append(month_start.strftime('%b %Y'))
        values.append(avg_hours)

    return jsonify({'labels': labels, 'data': values})


@public_bp.route('/api/chart/sla-compliance')
def get_sla_compliance_chart_data():
    """SLA compliance percentage per month for closed complaints."""
    try:
        filters = _parse_dashboard_filters(default_month_window=True)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400

    base_query = _apply_dashboard_filters(
        Complaint.query.filter(Complaint.resolved_at.isnot(None)),
        filters,
        date_field=Complaint.resolved_at,
        include_time_window=False
    )

    labels = []
    values = []
    for month_start in _iter_month_starts(filters['from_month_start'], filters['to_month_start']):
        month_end = _shift_month(month_start, 1)
        closed = base_query.filter(
            Complaint.resolved_at >= month_start,
            Complaint.resolved_at < month_end
        ).all()
        within = sum(1 for c in closed if c.sla_due_at and c.resolved_at and c.resolved_at <= c.sla_due_at)
        compliance = round((within / len(closed) * 100), 2) if closed else 0
        labels.append(month_start.strftime('%b %Y'))
        values.append(compliance)

    return jsonify({'labels': labels, 'data': values})


@public_bp.route('/api/public/data')
def public_data_api():
    """Public transparency dataset (aggregate only)."""
    stats = Complaint.get_stats()
    departments = []
    for dept in Department.query.all():
        q = Complaint.query.filter_by(department_id=dept.id)
        total = q.count()
        closed = q.filter_by(status='Closed').count()
        delayed = q.filter_by(status='Delayed').count()
        departments.append({
            'department': dept.name,
            'total': total,
            'closed': closed,
            'delayed': delayed,
            'resolution_rate': round((closed / total * 100), 2) if total else 0
        })
    return jsonify({'stats': stats, 'departments': departments})


@public_bp.route('/api/public/export/monthly.csv')
def export_monthly_csv():
    """Export monthly anonymized complaint data as CSV."""
    month_value = request.args.get('month', datetime.utcnow().strftime('%Y-%m'))
    try:
        month_start = datetime.strptime(month_value, '%Y-%m')
    except ValueError:
        return jsonify({'error': 'Invalid month format. Use YYYY-MM.'}), 400

    if month_start.month == 12:
        month_end = month_start.replace(year=month_start.year + 1, month=1)
    else:
        month_end = month_start.replace(month=month_start.month + 1)

    complaints = Complaint.query.filter(
        Complaint.submitted_at >= month_start,
        Complaint.submitted_at < month_end
    ).order_by(Complaint.submitted_at.asc()).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'tracking_id', 'department', 'service', 'status', 'priority',
        'submitted_at', 'resolved_at', 'reopen_count', 'citizen_rating'
    ])
    for complaint in complaints:
        writer.writerow([
            complaint.tracking_id,
            complaint.department.name if complaint.department else '',
            complaint.service.name if complaint.service else '',
            complaint.status,
            complaint.priority,
            complaint.submitted_at.isoformat() if complaint.submitted_at else '',
            complaint.resolved_at.isoformat() if complaint.resolved_at else '',
            complaint.reopen_count or 0,
            complaint.citizen_rating or ''
        ])

    csv_data = output.getvalue()
    output.close()
    filename = f'mibsp_public_export_{month_value}.csv'
    return Response(
        csv_data,
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )


@public_bp.route('/api/geo/heatmap')
def get_geo_heatmap_data():
    """Return geo-tagged complaint points for heatmap rendering."""
    maybe_run_sla_escalations()
    requested_limit = request.args.get('limit', type=int)
    max_points = int(current_app.config.get('GEO_HEATMAP_MAX_POINTS', 2500))
    if requested_limit is None or requested_limit <= 0:
        requested_limit = max_points
    requested_limit = min(requested_limit, max_points * 2)

    complaints = Complaint.query.filter(
        Complaint.location_lat.isnot(None),
        Complaint.location_lng.isnot(None)
    ).order_by(Complaint.submitted_at.desc()).limit(requested_limit).all()
    return jsonify([
        {
            'lat': complaint.location_lat,
            'lng': complaint.location_lng,
            'tracking_id': complaint.tracking_id,
            'status': complaint.status,
            'priority': complaint.priority,
            'state': complaint.state,
            'district': complaint.district,
            'city': complaint.city,
            'submitted_at': complaint.submitted_at.isoformat() if complaint.submitted_at else None
        } for complaint in complaints
    ])


@public_bp.route('/api/ai/assist', methods=['POST'])
def ai_assist():
    """
    AI assistant for complaint drafting and homepage guidance.
    Returns guidance only; does not store chat content.
    """
    if not request.is_json:
        return jsonify({'error': 'JSON request body required.'}), 400

    payload = request.get_json(silent=True) or {}
    message = (payload.get('message') or '').strip()
    assistant_mode = (payload.get('assistant') or '').strip().lower()
    description = (payload.get('description') or '').strip()
    department_id = payload.get('department_id')
    service_id = payload.get('service_id')

    if len(message) < 5:
        return jsonify({'error': 'Please provide a more specific question.'}), 400
    if len(message) > 1000:
        return jsonify({'error': 'Question is too long. Keep it under 1000 characters.'}), 400

    # Abuse guard for unauthenticated endpoint.
    allowed, rate_error = _enforce_ai_rate_limit()
    if not allowed:
        return jsonify({'error': rate_error}), 429

    department_name = None
    service_name = None
    if isinstance(department_id, int):
        department = db.session.get(Department, department_id)
        department_name = department.name if department else None
    if isinstance(service_id, int):
        service = db.session.get(Service, service_id)
        service_name = service.name if service else None

    api_key = current_app.config.get('OPENAI_API_KEY')
    model = current_app.config.get('OPENAI_MODEL', 'gpt-4o-mini')
    if not api_key:
        fallback = _fallback_ai_reply(
            assistant_mode, message, description, department_name, service_name
        )
        return jsonify({'reply': fallback, 'fallback': True}), 200

    try:
        from openai import OpenAI
    except ImportError:
        fallback = _fallback_ai_reply(
            assistant_mode, message, description, department_name, service_name
        )
        return jsonify({'reply': fallback, 'fallback': True}), 200

    if assistant_mode == 'homepage':
        system_prompt = (
            "You are the homepage help chatbot for the Municipal Integrity & Bribe-Free Service Portal. "
            "Help citizens use the portal effectively. "
            "Be concise, practical, and neutral. "
            "Do not request or encourage sharing personal identifiers. "
            "When relevant, guide users to these routes: /submit, /track, /dashboard, /geo-heatmap. "
            "Output plain text only."
        )
        user_prompt = (
            f"Citizen question: {message}\n\n"
            "Respond with:\n"
            "1) direct answer in 2-4 short sentences\n"
            "2) next best action in one line"
        )
    else:
        system_prompt = (
            "You assist citizens in drafting municipal complaints. "
            "Be concise, practical, and neutral. "
            "Do not request personal identifiers. "
            "Focus on facts: what happened, where, when, impact, and evidence. "
            "Output plain text."
        )
        user_prompt = (
            f"Citizen question: {message}\n"
            f"Department: {department_name or 'Not selected'}\n"
            f"Service: {service_name or 'Not selected'}\n"
            f"Current complaint draft: {description[:2000] if description else 'None'}\n\n"
            "Provide:\n"
            "1) quick guidance\n"
            "2) an improved complaint draft (120-220 words)\n"
            "3) a short checklist of missing details"
        )

    try:
        client = OpenAI(api_key=api_key)
        completion = client.chat.completions.create(
            model=model,
            temperature=0.2,
            max_tokens=500,
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_prompt},
            ],
        )
        reply = (completion.choices[0].message.content or '').strip()
        if not reply:
            return jsonify({'error': 'AI assistant returned an empty response.'}), 502
        return jsonify({'reply': reply}), 200
    except Exception:
        current_app.logger.exception('AI assistant request failed.')
        fallback = _fallback_ai_reply(
            assistant_mode, message, description, department_name, service_name
        )
        return jsonify({'reply': fallback, 'fallback': True}), 200


# =============================================================================
# HEALTH CHECK
# =============================================================================

@public_bp.route('/health')
def health_check():
    """Health check endpoint for monitoring."""
    try:
        # Test database connection
        db.session.execute(text('SELECT 1'))
        return jsonify({
            'status': 'healthy',
            'database': 'connected',
            'timestamp': datetime.utcnow().isoformat()
        }), 200
    except Exception as e:
        return jsonify({
            'status': 'unhealthy',
            'database': 'disconnected',
            'error': str(e),
            'timestamp': datetime.utcnow().isoformat()
        }), 503
