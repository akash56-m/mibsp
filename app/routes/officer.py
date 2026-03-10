"""
MIBSP Officer Routes
Officer dashboard and complaint management.
Officers can only access complaints in their department.
"""
from datetime import datetime
from flask import Blueprint, render_template, request, flash, redirect, url_for, session, jsonify, current_app
from sqlalchemy.orm import joinedload

from app import db
from app.models import User, Complaint, Department, AuditLog
from app.utils import officer_required, log_action, maybe_run_sla_escalations
from app.tasks import send_status_update_notification

officer_bp = Blueprint('officer', __name__)


@officer_bp.route('/dashboard')
@officer_required
def dashboard():
    """Officer dashboard with assigned complaints."""
    maybe_run_sla_escalations()
    user_id = session['user_id']
    department_id = session.get('department_id')
    
    # Get officer's complaints
    assigned_complaints = Complaint.query.filter_by(assigned_to=user_id)\
        .order_by(Complaint.submitted_at.desc()).all()
    
    # Get unassigned complaints in officer's department
    unassigned_complaints = Complaint.query.filter_by(
        department_id=department_id,
        assigned_to=None
    ).order_by(Complaint.submitted_at.desc()).limit(10).all()
    
    # Stats for officer
    stats = {
        'assigned': len(assigned_complaints),
        'pending': sum(1 for c in assigned_complaints if c.status == 'Pending'),
        'under_review': sum(1 for c in assigned_complaints if c.status == 'Under Review'),
        'delayed': sum(1 for c in assigned_complaints if c.status == 'Delayed'),
        'reopened': sum(1 for c in assigned_complaints if c.status == 'Reopened'),
        'high_priority': sum(1 for c in assigned_complaints if c.priority == 'High'),
        'closed': sum(1 for c in assigned_complaints if c.status == 'Closed')
    }
    
    return render_template('officer/dashboard.html',
                          assigned_complaints=assigned_complaints,
                          unassigned_complaints=unassigned_complaints,
                          stats=stats)


@officer_bp.route('/complaints')
def complaints_redirect():
    """Backward-compatible path kept for older bookmarks / external links."""
    return redirect(url_for('officer.dashboard'))


@officer_bp.route('/complaint/<tracking_id>')
@officer_required
def complaint_detail(tracking_id):
    """View complaint details."""
    complaint = Complaint.query.filter_by(tracking_id=tracking_id).first_or_404()
    
    # Check access permission
    user = db.session.get(User, session['user_id'])
    if not user.can_access_complaint(complaint):
        flash('You do not have permission to view this complaint.', 'danger')
        return redirect(url_for('officer.dashboard'))
    
    # Get audit logs for this complaint
    audit_logs = AuditLog.query.filter(
        AuditLog.details.contains(tracking_id)
    ).order_by(AuditLog.timestamp.desc()).limit(20).all()
    
    return render_template('officer/complaint_detail.html',
                          complaint=complaint,
                          audit_logs=audit_logs)


@officer_bp.route('/complaint/<tracking_id>/update', methods=['POST'])
@officer_required
def update_status(tracking_id):
    """Update complaint status."""
    complaint = Complaint.query.filter_by(tracking_id=tracking_id).first_or_404()
    
    # Check permission
    user = db.session.get(User, session['user_id'])
    if not user.can_access_complaint(complaint):
        flash('You do not have permission to update this complaint.', 'danger')
        return redirect(url_for('officer.dashboard'))
    
    # Get form data
    new_status = request.form.get('status')
    notes = request.form.get('notes', '').strip()
    
    # Validate status transition
    if not complaint.can_transition_to(new_status):
        flash(f"Cannot transition from '{complaint.status}' to '{new_status}'", 'danger')
        return redirect(url_for('officer.complaint_detail', tracking_id=tracking_id))
    
    # Update status
    try:
        old_status = complaint.status
        success, message = complaint.update_status(new_status, notes)
        
        if success:
            db.session.commit()
            
            # Log the action
            log_action('STATUS_UPDATE', 
                      details={
                          'tracking_id': tracking_id,
                          'old_status': old_status,
                          'new_status': new_status,
                          'notes': notes
                      }, user=user)
            
            # Trigger notification task (async)
            send_status_update_notification(tracking_id, new_status)
            
            flash(f'Status updated to {new_status}.', 'success')
        else:
            flash(message, 'danger')
            
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Status update error: {str(e)}')
        flash('Error updating status. Please try again.', 'danger')
    
    return redirect(url_for('officer.complaint_detail', tracking_id=tracking_id))


@officer_bp.route('/complaint/<tracking_id>/assign', methods=['POST'])
@officer_required
def assign_to_me(tracking_id):
    """Self-assign an unassigned complaint."""
    complaint = Complaint.query.filter_by(tracking_id=tracking_id).first_or_404()
    user = db.session.get(User, session['user_id'])
    
    # Check if in same department and unassigned
    if complaint.department_id != user.department_id:
        flash('This complaint is not in your department.', 'danger')
        return redirect(url_for('officer.dashboard'))
    
    if complaint.assigned_to is not None:
        flash('This complaint is already assigned.', 'warning')
        return redirect(url_for('officer.complaint_detail', tracking_id=tracking_id))
    
    # Assign
    try:
        complaint.assigned_to = user.id
        complaint.status = 'Under Review'
        db.session.commit()
        
        log_action('COMPLAINT_ASSIGNED',
                  details={
                      'tracking_id': tracking_id,
                      'assigned_to': user.username
                  }, user=user)
        
        flash('Complaint assigned to you.', 'success')
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Assignment error: {str(e)}')
        flash('Error assigning complaint. Please try again.', 'danger')
    
    return redirect(url_for('officer.complaint_detail', tracking_id=tracking_id))


@officer_bp.route('/complaint/<tracking_id>/notes', methods=['POST'])
@officer_required
def add_notes(tracking_id):
    """Add investigation notes to a complaint."""
    complaint = Complaint.query.filter_by(tracking_id=tracking_id).first_or_404()
    user = db.session.get(User, session['user_id'])
    
    if not user.can_access_complaint(complaint):
        flash('You do not have permission to modify this complaint.', 'danger')
        return redirect(url_for('officer.dashboard'))
    
    notes = request.form.get('notes', '').strip()
    
    if not notes:
        flash('Please enter notes.', 'warning')
        return redirect(url_for('officer.complaint_detail', tracking_id=tracking_id))
    
    try:
        # Append to existing notes
        timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M')
        new_note = f"[{timestamp}] {user.username}: {notes}"
        
        if complaint.resolution_notes:
            complaint.resolution_notes += f"\n\n{new_note}"
        else:
            complaint.resolution_notes = new_note
        
        db.session.commit()
        
        log_action('NOTES_ADDED',
                  details={
                      'tracking_id': tracking_id,
                      'note_preview': notes[:100]
                  }, user=user)
        
        flash('Notes added successfully.', 'success')
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Notes error: {str(e)}')
        flash('Error adding notes. Please try again.', 'danger')
    
    return redirect(url_for('officer.complaint_detail', tracking_id=tracking_id))


@officer_bp.route('/api/my-stats')
@officer_required
def get_my_stats():
    """API endpoint for officer's personal stats."""
    maybe_run_sla_escalations()
    user_id = session['user_id']
    
    assigned = Complaint.query.filter_by(assigned_to=user_id)
    
    stats = {
        'total_assigned': assigned.count(),
        'pending': assigned.filter_by(status='Pending').count(),
        'under_review': assigned.filter_by(status='Under Review').count(),
        'action_taken': assigned.filter_by(status='Action Taken').count(),
        'delayed': assigned.filter_by(status='Delayed').count(),
        'reopened': assigned.filter_by(status='Reopened').count(),
        'high_priority': assigned.filter_by(priority='High').count(),
        'closed': assigned.filter_by(status='Closed').count()
    }
    
    return jsonify(stats)
