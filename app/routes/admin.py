"""
MIBSP Admin Routes
Administrator panel for system management.
Admins have full access to all features.
"""
import csv
import io
from flask import Blueprint, render_template, request, flash, redirect, url_for, session, jsonify, current_app, Response
from sqlalchemy.orm import joinedload
from datetime import datetime, timedelta

from app import db
from app.models import User, Department, Service, Complaint, AuditLog
from app.utils import admin_required, log_action, maybe_run_sla_escalations
from app.tasks import send_status_update_notification

admin_bp = Blueprint('admin', __name__)


def _build_officer_performance_records(limit=None):
    """Calculate officer performance index records."""
    officers = User.query.filter(
        User.role.in_(['officer', 'zonal_officer', 'commissioner']),
        User.is_active.is_(True)
    ).all()

    records = []
    for officer in officers:
        handled = Complaint.query.filter_by(assigned_to=officer.id).all()
        total = len(handled)
        closed = [c for c in handled if c.status == 'Closed']
        avg_hours = None
        if closed:
            avg_hours = sum(c.get_resolution_time() or 0 for c in closed) / len(closed)
        rated = [c.citizen_rating for c in closed if c.citizen_rating]
        avg_rating = (sum(rated) / len(rated)) if rated else 0
        speed_component = 0
        if avg_hours is not None:
            speed_component = max(0, 100 - min(avg_hours, 100))
        closure_component = (len(closed) / total * 100) if total else 0
        rating_component = (avg_rating / 5 * 100) if rated else 0
        performance_index = round(
            (speed_component * 0.4) + (closure_component * 0.4) + (rating_component * 0.2),
            2
        )

        records.append({
            'username': officer.username,
            'role': officer.role,
            'handled': total,
            'closed': len(closed),
            'avg_resolution_hours': round(avg_hours, 2) if avg_hours is not None else None,
            'avg_rating': round(avg_rating, 2) if rated else None,
            'performance_index': performance_index
        })

    records.sort(key=lambda r: r['performance_index'], reverse=True)
    if isinstance(limit, int) and limit > 0:
        return records[:limit]
    return records


# =============================================================================
# DASHBOARD
# =============================================================================

@admin_bp.route('/dashboard')
@admin_required
def dashboard():
    """Admin dashboard with system overview."""
    maybe_run_sla_escalations()

    # Overall stats
    stats = Complaint.get_stats()
    
    # Additional metrics
    total_officers = User.query.filter(
        User.role.in_(['officer', 'zonal_officer', 'commissioner'])
    ).count()
    total_departments = Department.query.count()
    
    # Recent complaints
    recent_complaints = Complaint.query.order_by(
        Complaint.submitted_at.desc()
    ).limit(10).all()
    
    # Recent audit logs
    recent_logs = AuditLog.query.order_by(
        AuditLog.timestamp.desc()
    ).limit(10).all()
    top_officers = _build_officer_performance_records(limit=8)
    
    # Department performance
    departments = Department.query.all()
    dept_performance = []
    for dept in departments:
        dept_complaints = Complaint.query.filter_by(department_id=dept.id)
        total = dept_complaints.count()
        closed = dept_complaints.filter_by(status='Closed').count()
        delayed = dept_complaints.filter_by(status='Delayed').count()
        
        # Calculate average resolution time
        resolved = dept_complaints.filter(Complaint.resolved_at.isnot(None)).all()
        avg_hours = None
        if resolved:
            total_hours = sum(c.get_resolution_time() or 0 for c in resolved)
            avg_hours = round(total_hours / len(resolved), 2)

        rated = [c.citizen_rating for c in resolved if c.citizen_rating]
        avg_rating = round(sum(rated) / len(rated), 2) if rated else None
        resolution_rate = round((closed / total * 100), 1) if total > 0 else 0
        delay_penalty = round((delayed / total * 100) * 0.5, 1) if total > 0 else 0
        performance_score = round(max(resolution_rate - delay_penalty, 0), 1)
        
        dept_performance.append({
            'name': dept.name,
            'total': total,
            'closed': closed,
            'delayed': delayed,
            'resolution_rate': resolution_rate,
            'performance_score': performance_score,
            'avg_rating': avg_rating,
            'avg_resolution_hours': avg_hours
        })
    
    return render_template('admin/dashboard.html',
                          stats=stats,
                          total_officers=total_officers,
                          total_departments=total_departments,
                          recent_complaints=recent_complaints,
                          recent_logs=recent_logs,
                          dept_performance=dept_performance,
                          top_officers=top_officers)


# =============================================================================
# COMPLAINT MANAGEMENT
# =============================================================================

@admin_bp.route('/complaints')
@admin_required
def complaints():
    """List all complaints with filtering."""
    maybe_run_sla_escalations()

    # Get filter parameters
    status = request.args.get('status', '')
    department_id = request.args.get('department_id', type=int)
    search = request.args.get('search', '').strip()
    
    # Build query
    query = Complaint.query
    
    if status:
        query = query.filter_by(status=status)
    
    if department_id:
        query = query.filter_by(department_id=department_id)
    
    if search:
        query = query.filter(
            db.or_(
                Complaint.tracking_id.ilike(f'%{search}%'),
                Complaint.description.ilike(f'%{search}%')
            )
        )
    
    # Pagination
    page = request.args.get('page', 1, type=int)
    per_page = 20
    pagination = query.order_by(Complaint.submitted_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    
    departments = Department.query.all()
    
    return render_template('admin/complaints.html',
                          complaints=pagination.items,
                          pagination=pagination,
                          departments=departments,
                          filters={
                              'status': status,
                              'department_id': department_id,
                              'search': search
                          })


@admin_bp.route('/complaint/<tracking_id>')
@admin_required
def complaint_detail(tracking_id):
    """View complaint details as admin."""
    maybe_run_sla_escalations()
    complaint = Complaint.query.filter_by(tracking_id=tracking_id).first_or_404()
    
    # Get audit logs for this complaint
    audit_logs = AuditLog.query.filter(
        AuditLog.details.contains(tracking_id)
    ).order_by(AuditLog.timestamp.desc()).all()
    
    # Get all officers for reassignment
    officers = User.query.filter(
        User.role.in_(['officer', 'zonal_officer', 'commissioner']),
        User.is_active.is_(True)
    ).all()
    
    return render_template('admin/complaint_detail.html',
                          complaint=complaint,
                          audit_logs=audit_logs,
                          officers=officers)


@admin_bp.route('/complaint/<tracking_id>/assign', methods=['POST'])
@admin_required
def assign_complaint(tracking_id):
    """Assign complaint to an officer."""
    complaint = Complaint.query.filter_by(tracking_id=tracking_id).first_or_404()
    
    officer_id = request.form.get('officer_id', type=int)
    
    if not officer_id:
        flash('Please select an officer.', 'warning')
        return redirect(url_for('admin.complaint_detail', tracking_id=tracking_id))
    
    officer = db.session.get(User, officer_id)
    if not officer or officer.role not in ['officer', 'zonal_officer', 'commissioner']:
        flash('Invalid officer selected.', 'danger')
        return redirect(url_for('admin.complaint_detail', tracking_id=tracking_id))
    
    try:
        old_assignee = complaint.assigned_officer.username if complaint.assigned_officer else 'Unassigned'
        
        complaint.assigned_to = officer_id
        if complaint.status == 'Pending':
            complaint.status = 'Under Review'
        
        db.session.commit()
        
        log_action('COMPLAINT_ASSIGNED_BY_ADMIN',
                  details={
                      'tracking_id': tracking_id,
                      'assigned_to': officer.username,
                      'previous_assignee': old_assignee
                  })
        
        flash(f'Complaint assigned to {officer.username}.', 'success')
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Assignment error: {str(e)}')
        flash('Error assigning complaint.', 'danger')
    
    return redirect(url_for('admin.complaint_detail', tracking_id=tracking_id))


@admin_bp.route('/complaint/<tracking_id>/update', methods=['POST'])
@admin_required
def update_complaint_status(tracking_id):
    """Update complaint status as admin."""
    complaint = Complaint.query.filter_by(tracking_id=tracking_id).first_or_404()
    
    new_status = request.form.get('status')
    notes = request.form.get('notes', '').strip()
    
    if not complaint.can_transition_to(new_status):
        flash(f"Cannot transition from '{complaint.status}' to '{new_status}'", 'danger')
        return redirect(url_for('admin.complaint_detail', tracking_id=tracking_id))
    
    try:
        old_status = complaint.status
        success, message = complaint.update_status(new_status, notes)
        
        if success:
            db.session.commit()
            
            log_action('STATUS_UPDATE_BY_ADMIN',
                      details={
                          'tracking_id': tracking_id,
                          'old_status': old_status,
                          'new_status': new_status
                      })
            
            send_status_update_notification(tracking_id, new_status)
            
            flash('Status updated successfully.', 'success')
        else:
            flash(message, 'danger')
            
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Status update error: {str(e)}')
        flash('Error updating status.', 'danger')
    
    return redirect(url_for('admin.complaint_detail', tracking_id=tracking_id))


# =============================================================================
# USER MANAGEMENT
# =============================================================================

@admin_bp.route('/complaint/<tracking_id>/evidence')
@admin_required
def download_evidence(tracking_id):
    """Download evidence file for a complaint."""
    import os
    from flask import send_from_directory, abort
    
    complaint = Complaint.query.filter_by(tracking_id=tracking_id).first_or_404()
    
    if not complaint.evidence_path:
        flash('No evidence file attached to this complaint.', 'warning')
        return redirect(url_for('admin.complaint_detail', tracking_id=tracking_id))
    
    upload_folder = current_app.config.get('UPLOAD_FOLDER', 'uploads')
    
    if not os.path.exists(os.path.join(upload_folder, complaint.evidence_path)):
        flash('Evidence file not found on server.', 'danger')
        return redirect(url_for('admin.complaint_detail', tracking_id=tracking_id))
    
    return send_from_directory(
        upload_folder,
        complaint.evidence_path,
        as_attachment=True
    )


@admin_bp.route("/officers")
@admin_required
def officers():
    """List all officers."""
    officers = User.query.filter(
        User.role.in_(['officer', 'zonal_officer', 'commissioner'])
    ).all()
    departments = Department.query.all()
    
    return render_template('admin/officers.html', 
                          officers=officers,
                          departments=departments)


@admin_bp.route('/officers/create', methods=['POST'])
@admin_required
def create_officer():
    """Create new officer account."""
    username = request.form.get('username', '').strip()
    email = request.form.get('email', '').strip()
    password = request.form.get('password', '')
    role = request.form.get('role', 'officer').strip()
    department_id = request.form.get('department_id', type=int)
    
    # Validation
    errors = []
    if not username or len(username) < 3:
        errors.append('Username must be at least 3 characters.')
    if not password or len(password) < 8:
        errors.append('Password must be at least 8 characters.')
    if role not in ['officer', 'zonal_officer', 'commissioner']:
        errors.append('Invalid role selected.')
    if User.query.filter_by(username=username).first():
        errors.append('Username already exists.')
    
    if errors:
        for error in errors:
            flash(error, 'danger')
        return redirect(url_for('admin.officers'))
    
    try:
        officer = User(
            username=username,
            email=email or None,
            role=role,
            department_id=department_id,
            is_active=True
        )
        officer.set_password(password)
        
        db.session.add(officer)
        db.session.commit()
        
        log_action('OFFICER_CREATED',
                  details={
                      'username': username,
                      'role': role,
                      'department_id': department_id
                  })
        
        flash(f'Officer {username} created successfully.', 'success')
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Officer creation error: {str(e)}')
        flash('Error creating officer.', 'danger')
    
    return redirect(url_for('admin.officers'))


@admin_bp.route('/officers/<int:officer_id>/toggle', methods=['POST'])
@admin_required
def toggle_officer(officer_id):
    """Enable/disable officer account."""
    officer = User.query.get_or_404(officer_id)
    
    if officer.role not in ['officer', 'zonal_officer', 'commissioner']:
        flash('Invalid user.', 'danger')
        return redirect(url_for('admin.officers'))
    
    try:
        officer.is_active = not officer.is_active
        db.session.commit()
        
        status = 'enabled' if officer.is_active else 'disabled'
        
        log_action('OFFICER_TOGGLED',
                  details={
                      'username': officer.username,
                      'new_status': status
                  })
        
        flash(f'Officer {officer.username} {status}.', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash('Error updating officer.', 'danger')
    
    return redirect(url_for('admin.officers'))


@admin_bp.route('/officers/<int:officer_id>/reset-password', methods=['POST'])
@admin_required
def reset_officer_password(officer_id):
    """Reset officer password."""
    officer = User.query.get_or_404(officer_id)
    
    if officer.role not in ['officer', 'zonal_officer', 'commissioner']:
        flash('Invalid user.', 'danger')
        return redirect(url_for('admin.officers'))
    
    new_password = request.form.get('new_password', '')
    
    if not new_password or len(new_password) < 8:
        flash('Password must be at least 8 characters.', 'danger')
        return redirect(url_for('admin.officers'))
    
    try:
        officer.set_password(new_password)
        db.session.commit()
        
        log_action('OFFICER_PASSWORD_RESET',
                  details={'username': officer.username})
        
        flash(f'Password reset for {officer.username}.', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash('Error resetting password.', 'danger')
    
    return redirect(url_for('admin.officers'))


# =============================================================================
# DEPARTMENT MANAGEMENT
# =============================================================================

@admin_bp.route('/departments')
@admin_required
def departments():
    """List all departments."""
    departments = Department.query.all()
    return render_template('admin/departments.html', departments=departments)


@admin_bp.route('/departments/create', methods=['POST'])
@admin_required
def create_department():
    """Create new department."""
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    
    if not name:
        flash('Department name is required.', 'danger')
        return redirect(url_for('admin.departments'))
    
    if Department.query.filter_by(name=name).first():
        flash('Department already exists.', 'danger')
        return redirect(url_for('admin.departments'))
    
    try:
        dept = Department(name=name, description=description)
        db.session.add(dept)
        db.session.commit()
        
        log_action('DEPARTMENT_CREATED',
                  details={'name': name})
        
        flash(f'Department {name} created.', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash('Error creating department.', 'danger')
    
    return redirect(url_for('admin.departments'))


@admin_bp.route('/departments/<int:dept_id>/services', methods=['POST'])
@admin_required
def add_service(dept_id):
    """Add service to department."""
    dept = Department.query.get_or_404(dept_id)
    
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    sla_days = request.form.get('sla_days', type=int) or 7
    
    if not name:
        flash('Service name is required.', 'danger')
        return redirect(url_for('admin.departments'))
    if sla_days < 1 or sla_days > 60:
        flash('SLA days must be between 1 and 60.', 'danger')
        return redirect(url_for('admin.departments'))
    
    try:
        service = Service(name=name, description=description, department_id=dept_id, sla_days=sla_days)
        db.session.add(service)
        db.session.commit()
        
        log_action('SERVICE_CREATED',
                  details={
                      'name': name,
                      'sla_days': sla_days,
                      'department': dept.name
                  })
        
        flash(f'Service {name} added to {dept.name}.', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash('Error adding service.', 'danger')
    
    return redirect(url_for('admin.departments'))


# =============================================================================
# AUDIT LOGS
# =============================================================================

@admin_bp.route('/audit-logs')
@admin_required
def audit_logs():
    """View audit logs with filtering."""
    # Get filter parameters
    action = request.args.get('action', '')
    username = request.args.get('username', '').strip()
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    
    # Build query
    query = AuditLog.query
    
    if action:
        query = query.filter_by(action=action)
    
    if username:
        query = query.filter(AuditLog.username.ilike(f'%{username}%'))
    
    if date_from:
        try:
            from_date = datetime.strptime(date_from, '%Y-%m-%d')
            query = query.filter(AuditLog.timestamp >= from_date)
        except ValueError:
            pass
    
    if date_to:
        try:
            to_date = datetime.strptime(date_to, '%Y-%m-%d')
            to_date = to_date.replace(hour=23, minute=59, second=59)
            query = query.filter(AuditLog.timestamp <= to_date)
        except ValueError:
            pass
    
    # Pagination
    page = request.args.get('page', 1, type=int)
    per_page = 50
    pagination = query.order_by(AuditLog.timestamp.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    
    # Get unique actions for filter dropdown
    actions = db.session.query(AuditLog.action).distinct().all()
    actions = [a[0] for a in actions]
    
    return render_template('admin/audit_logs.html',
                          logs=pagination.items,
                          pagination=pagination,
                          actions=actions,
                          filters={
                              'action': action,
                              'username': username,
                              'date_from': date_from,
                              'date_to': date_to
                          })


@admin_bp.route('/audit-logs/verify')
@admin_required
def verify_audit_logs():
    """Verify hash chain integrity of audit logs."""
    logs = AuditLog.query.order_by(AuditLog.id.asc()).all()
    if not logs:
        return jsonify({'valid': True, 'checked': 0, 'message': 'No logs to verify.'})

    previous_hash = None
    for log in logs:
        if not log.verify_integrity():
            return jsonify({
                'valid': False,
                'checked': log.id,
                'message': 'Row hash mismatch detected.',
                'broken_log_id': log.id
            }), 409
        if previous_hash is not None and log.previous_hash != previous_hash:
            return jsonify({
                'valid': False,
                'checked': log.id,
                'message': 'Chain linkage mismatch detected.',
                'broken_log_id': log.id
            }), 409
        previous_hash = log.row_hash

    return jsonify({
        'valid': True,
        'checked': len(logs),
        'message': 'Audit chain verified successfully.'
    })


@admin_bp.route('/audit-logs/rebuild', methods=['POST'])
@admin_required
def rebuild_audit_chain():
    """Recompute audit log chain hashes and previous hash pointers."""
    if session.get('role') != 'admin':
        flash('Only admins can rebuild the audit chain.', 'danger')
        return redirect(url_for('admin.audit_logs'))

    result = AuditLog.rebuild_chain(dry_run=False)
    log_action(
        'AUDIT_CHAIN_REBUILT',
        user=db.session.get(User, session['user_id'])
    )
    flash(
        f'Audit chain rebuilt for {result["total"]} logs; {result["repaired"]} entries updated.',
        'success' if result['repaired'] else 'info'
    )

    return redirect(url_for('admin.audit_logs'))


# =============================================================================
# API ENDPOINTS
# =============================================================================

@admin_bp.route('/api/system-stats')
@admin_required
def get_system_stats():
    """API endpoint for system statistics."""
    maybe_run_sla_escalations()

    # Complaint stats
    complaint_stats = Complaint.get_stats()
    
    # User stats
    user_stats = {
        'total_users': User.query.count(),
        'officers': User.query.filter(User.role.in_(['officer', 'zonal_officer', 'commissioner'])).count(),
        'admins': User.query.filter_by(role='admin').count(),
        'active_users': User.query.filter_by(is_active=True).count()
    }
    
    # Department stats
    dept_stats = []
    for dept in Department.query.all():
        complaints = Complaint.query.filter_by(department_id=dept.id)
        dept_stats.append({
            'name': dept.name,
            'total': complaints.count(),
            'pending': complaints.filter_by(status='Pending').count(),
            'closed': complaints.filter_by(status='Closed').count()
        })
    
    # Activity in last 24 hours
    yesterday = datetime.utcnow() - timedelta(days=1)
    recent_activity = {
        'new_complaints': Complaint.query.filter(Complaint.submitted_at >= yesterday).count(),
        'resolved': Complaint.query.filter(Complaint.resolved_at >= yesterday).count(),
        'admin_actions': AuditLog.query.filter(
            AuditLog.timestamp >= yesterday,
            AuditLog.role == 'admin'
        ).count()
    }
    
    return jsonify({
        'complaints': complaint_stats,
        'users': user_stats,
        'departments': dept_stats,
        'recent_activity': recent_activity
    })


@admin_bp.route('/api/analytics/sentiment')
@admin_required
def get_sentiment_analytics():
    """Get sentiment and urgency distribution for dashboard widgets."""
    total = Complaint.query.count()
    negative = Complaint.query.filter_by(ai_sentiment='negative').count()
    neutral = Complaint.query.filter_by(ai_sentiment='neutral').count()
    positive = Complaint.query.filter_by(ai_sentiment='positive').count()
    urgent = Complaint.query.filter_by(ai_urgent=True).count()
    reopened = Complaint.query.filter(Complaint.reopen_count > 0).count()

    def pct(value):
        return round((value / total * 100), 2) if total else 0

    return jsonify({
        'total': total,
        'negative_percent': pct(negative),
        'neutral_percent': pct(neutral),
        'positive_percent': pct(positive),
        'urgent_percent': pct(urgent),
        'reopened_percent': pct(reopened)
    })


@admin_bp.route('/api/analytics/service-trends')
@admin_required
def get_service_trends():
    """Top recurring service issues for analytics."""
    from sqlalchemy import func

    top_services = db.session.query(
        Service.name,
        func.count(Complaint.id).label('count')
    ).join(Complaint, Complaint.service_id == Service.id)\
        .group_by(Service.id, Service.name)\
        .order_by(func.count(Complaint.id).desc())\
        .limit(5).all()

    return jsonify({
        'labels': [row[0] for row in top_services],
        'data': [row[1] for row in top_services]
    })


@admin_bp.route('/api/analytics/officer-performance')
@admin_required
def get_officer_performance():
    """Officer performance index by workload, speed, and citizen ratings."""
    return jsonify(_build_officer_performance_records())


@admin_bp.route('/export/complaints.csv')
@admin_required
def export_complaints_csv():
    """Export complaints with analytics fields for governance/RTI workflows."""
    complaints = Complaint.query.order_by(Complaint.submitted_at.desc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'tracking_id', 'department', 'service', 'status', 'priority',
        'ai_sentiment', 'ai_urgent', 'escalation_level', 'reopen_count',
        'submitted_at', 'resolved_at', 'citizen_rating'
    ])

    for complaint in complaints:
        writer.writerow([
            complaint.tracking_id,
            complaint.department.name if complaint.department else '',
            complaint.service.name if complaint.service else '',
            complaint.status,
            complaint.priority,
            complaint.ai_sentiment,
            complaint.ai_urgent,
            complaint.escalation_level,
            complaint.reopen_count,
            complaint.submitted_at.isoformat() if complaint.submitted_at else '',
            complaint.resolved_at.isoformat() if complaint.resolved_at else '',
            complaint.citizen_rating or ''
        ])

    csv_data = output.getvalue()
    output.close()
    filename = f'mibsp_admin_export_{datetime.utcnow().strftime("%Y%m%d_%H%M%S")}.csv'
    return Response(
        csv_data,
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )
