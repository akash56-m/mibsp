"""
MIBSP Database Models
All SQLAlchemy models for the Municipal Integrity & Bribe-Free Service Portal.
"""
from datetime import datetime, timedelta
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
import hashlib
import json

from app import db


class Department(db.Model):
    """
    Government department/ward entity.
    Examples: Water Supply, Roads & Infrastructure, Public Health, etc.
    """
    __tablename__ = 'departments'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    description = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    services = db.relationship('Service', backref='department', lazy='dynamic',
                               cascade='all, delete-orphan')
    users = db.relationship('User', backref='department', lazy='dynamic')
    complaints = db.relationship('Complaint', backref='department', lazy='dynamic')
    
    def __repr__(self):
        return f'<Department {self.name}>'
    
    def to_dict(self):
        """Serialize department to dictionary."""
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'service_count': self.services.count(),
            'complaint_count': self.complaints.count()
        }


class Service(db.Model):
    """
    Service offered by a department.
    Citizens select department first, then service via AJAX.
    """
    __tablename__ = 'services'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey('departments.id'), nullable=False, index=True)
    description = db.Column(db.Text, nullable=True)
    sla_days = db.Column(db.Integer, nullable=False, default=7)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    complaints = db.relationship('Complaint', backref='service', lazy='dynamic')
    
    def __repr__(self):
        return f'<Service {self.name} ({self.department.name if self.department else "No Dept"})>'
    
    def to_dict(self):
        """Serialize service to dictionary."""
        return {
            'id': self.id,
            'name': self.name,
            'department_id': self.department_id,
            'description': self.description,
            'sla_days': self.sla_days,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class User(db.Model):
    """
    System user - Admin or Officer.
    Citizens do NOT have accounts (anonymous submission).
    """
    __tablename__ = 'users'
    
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), nullable=False, unique=True, index=True)
    email = db.Column(db.String(120), unique=True, nullable=True)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='officer')  # 'admin' or 'officer'
    department_id = db.Column(db.Integer, db.ForeignKey('departments.id'), nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    failed_login_attempts = db.Column(db.Integer, default=0, nullable=False)
    locked_until = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime, nullable=True)
    
    # Relationships
    assigned_complaints = db.relationship('Complaint', backref='assigned_officer',
                                          foreign_keys='Complaint.assigned_to', lazy='dynamic')
    audit_logs = db.relationship('AuditLog', backref='user', lazy='dynamic')
    
    def __repr__(self):
        return f'<User {self.username} ({self.role})>'
    
    def set_password(self, password):
        """Hash and set password using PBKDF2-SHA256."""
        self.password_hash = generate_password_hash(password, method='pbkdf2:sha256', salt_length=16)
    
    def check_password(self, password):
        """Verify password against hash."""
        return check_password_hash(self.password_hash, password)
    
    def is_admin(self):
        """Check if user has admin role."""
        return self.role == 'admin'
    
    def is_officer(self):
        """Check if user has officer role."""
        return self.role in ['officer', 'zonal_officer', 'commissioner']

    def is_locked(self):
        """Check if account is temporarily locked due to failed logins."""
        return self.locked_until is not None and self.locked_until > datetime.utcnow()

    def register_failed_login(self, threshold=5, lock_minutes=15):
        """Increment failed login attempts and lock account if threshold reached."""
        self.failed_login_attempts = (self.failed_login_attempts or 0) + 1
        if self.failed_login_attempts >= threshold:
            self.locked_until = datetime.utcnow() + timedelta(minutes=lock_minutes)

    def reset_login_failures(self):
        """Clear failed login counters on successful login."""
        self.failed_login_attempts = 0
        self.locked_until = None
    
    def can_access_complaint(self, complaint):
        """Check if user can access/modify a specific complaint."""
        if self.is_admin():
            return True
        if self.role == 'commissioner':
            return True
        if self.role in ['officer', 'zonal_officer'] and complaint.department_id == self.department_id:
            if complaint.assigned_to is None or complaint.assigned_to == self.id:
                return True
        return False
    
    def update_last_login(self):
        """Update last login timestamp."""
        self.last_login = datetime.utcnow()
        self.reset_login_failures()
        db.session.commit()
    
    def to_dict(self):
        """Serialize user to dictionary (safe - no password)."""
        return {
            'id': self.id,
            'username': self.username,
            'email': self.email,
            'role': self.role,
            'department_id': self.department_id,
            'department_name': self.department.name if self.department else None,
            'is_active': self.is_active,
            'failed_login_attempts': self.failed_login_attempts,
            'locked_until': self.locked_until.isoformat() if self.locked_until else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_login': self.last_login.isoformat() if self.last_login else None
        }


class Complaint(db.Model):
    """
    Citizen complaint - completely anonymous.
    No PII stored (no name, phone, email, IP address).
    """
    __tablename__ = 'complaints'
    
    id = db.Column(db.Integer, primary_key=True)
    tracking_id = db.Column(db.String(12), nullable=False, unique=True, index=True)
    service_id = db.Column(db.Integer, db.ForeignKey('services.id'), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey('departments.id'), nullable=False, index=True)
    description = db.Column(db.Text, nullable=False)
    evidence_path = db.Column(db.String(256), nullable=True)
    status = db.Column(db.String(30), default='Pending', nullable=False, index=True)
    assigned_to = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    escalation_level = db.Column(db.Integer, default=0, nullable=False)
    sla_due_at = db.Column(db.DateTime, nullable=True, index=True)
    delayed_at = db.Column(db.DateTime, nullable=True)
    reopen_count = db.Column(db.Integer, default=0, nullable=False)
    citizen_rating = db.Column(db.Integer, nullable=True)
    citizen_feedback = db.Column(db.Text, nullable=True)
    feedback_submitted_at = db.Column(db.DateTime, nullable=True)
    priority = db.Column(db.String(20), default='Normal', nullable=False, index=True)
    ai_category = db.Column(db.String(80), nullable=True)
    ai_sentiment = db.Column(db.String(20), default='neutral', nullable=False)
    ai_urgent = db.Column(db.Boolean, default=False, nullable=False)
    state = db.Column(db.String(80), nullable=True, index=True)
    district = db.Column(db.String(120), nullable=True, index=True)
    city = db.Column(db.String(120), nullable=True, index=True)
    location_lat = db.Column(db.Float, nullable=True)
    location_lng = db.Column(db.Float, nullable=True)
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    resolved_at = db.Column(db.DateTime, nullable=True)
    resolution_notes = db.Column(db.Text, nullable=True)
    
    # Valid status transitions
    VALID_STATUSES = ['Pending', 'Under Review', 'Action Taken', 'Delayed', 'Reopened', 'Closed']
    ACTIVE_STATUSES = ['Pending', 'Under Review', 'Action Taken', 'Delayed', 'Reopened']
    STATUS_FLOW = {
        'Pending': ['Under Review', 'Delayed', 'Closed'],
        'Under Review': ['Action Taken', 'Delayed', 'Closed'],
        'Action Taken': ['Delayed', 'Closed'],
        'Delayed': ['Under Review', 'Action Taken', 'Closed'],
        'Reopened': ['Under Review', 'Action Taken', 'Delayed', 'Closed'],
        'Closed': ['Reopened']
    }
    
    def __repr__(self):
        return f'<Complaint {self.tracking_id} ({self.status})>'
    
    def can_transition_to(self, new_status):
        """Check if status transition is valid."""
        return new_status in self.STATUS_FLOW.get(self.status, [])

    def initialize_sla_due(self):
        """Initialize SLA due date from service SLA config."""
        if self.sla_due_at is None and self.submitted_at and self.service:
            self.sla_due_at = self.submitted_at + timedelta(days=self.service.sla_days or 7)
        return self.sla_due_at

    def is_overdue(self):
        """Check if complaint has exceeded SLA and is still open."""
        due = self.initialize_sla_due()
        if not due:
            return False
        return self.status in self.ACTIVE_STATUSES and due < datetime.utcnow()

    def get_escalation_role(self):
        """Resolve hierarchy role from escalation level."""
        if self.escalation_level <= 0:
            return 'officer'
        if self.escalation_level == 1:
            return 'zonal_officer'
        return 'commissioner'

    def assign_by_escalation_hierarchy(self):
        """Assign complaint based on escalation hierarchy."""
        target_role = self.get_escalation_role()
        query = User.query.filter_by(role=target_role, is_active=True)
        if target_role != 'commissioner':
            query = query.filter_by(department_id=self.department_id)

        candidates = query.all()
        if not candidates and target_role != 'commissioner':
            candidates = User.query.filter_by(role='commissioner', is_active=True).all()

        if not candidates:
            return None

        def load_count(user):
            return Complaint.query.filter(
                Complaint.assigned_to == user.id,
                Complaint.status != 'Closed'
            ).count()

        assignee = min(candidates, key=load_count)
        self.assigned_to = assignee.id
        return assignee
    
    def update_status(self, new_status, notes=None, user=None):
        """
        Update complaint status with validation.
        Returns tuple (success: bool, message: str)
        """
        if not self.can_transition_to(new_status):
            return False, f"Cannot transition from '{self.status}' to '{new_status}'"
        
        old_status = self.status
        self.status = new_status
        self.updated_at = datetime.utcnow()
        
        if new_status == 'Closed':
            self.resolved_at = datetime.utcnow()
        elif new_status in self.ACTIVE_STATUSES:
            self.resolved_at = None

        if new_status == 'Reopened':
            self.reopen_count = (self.reopen_count or 0) + 1
            self.escalation_level = min((self.escalation_level or 0) + 1, 2)
            self.initialize_sla_due()
        
        if notes:
            self.resolution_notes = notes
        
        return True, f"Status updated from '{old_status}' to '{new_status}'"
    
    def get_resolution_time(self):
        """Calculate resolution time in hours."""
        if self.resolved_at and self.submitted_at:
            delta = self.resolved_at - self.submitted_at
            return round(delta.total_seconds() / 3600, 2)
        return None

    def resolution_days(self):
        """Calculate resolution time in days (used in templates)."""
        if self.resolved_at and self.submitted_at:
            return (self.resolved_at - self.submitted_at).days
        return None

    def submit_citizen_feedback(self, rating, feedback=''):
        """Store anonymous post-closure rating and feedback."""
        if self.status != 'Closed':
            return False, 'Feedback can be submitted only after closure.'
        if rating < 1 or rating > 5:
            return False, 'Rating must be between 1 and 5.'

        self.citizen_rating = rating
        self.citizen_feedback = feedback.strip() if feedback else None
        self.feedback_submitted_at = datetime.utcnow()
        return True, 'Feedback submitted successfully.'

    def reopen(self, reason):
        """Reopen a closed complaint for further review."""
        reason = (reason or '').strip()
        if self.status != 'Closed':
            return False, 'Only closed complaints can be reopened.'
        if len(reason) < 10:
            return False, 'Please provide at least 10 characters explaining why to reopen.'

        self.status = 'Reopened'
        self.updated_at = datetime.utcnow()
        self.resolved_at = None
        self.reopen_count = (self.reopen_count or 0) + 1
        self.escalation_level = min((self.escalation_level or 0) + 1, 2)
        self.initialize_sla_due()
        self.sla_due_at = datetime.utcnow() + timedelta(days=self.service.sla_days or 7)

        note = f"[Citizen Reopen] {reason}"
        if self.resolution_notes:
            self.resolution_notes += f"\n\n{note}"
        else:
            self.resolution_notes = note

        self.assign_by_escalation_hierarchy()
        return True, 'Complaint reopened successfully.'
    
    def to_dict(self, include_details=False):
        """Serialize complaint to dictionary."""
        data = {
            'id': self.id,
            'tracking_id': self.tracking_id,
            'service_id': self.service_id,
            'service_name': self.service.name if self.service else None,
            'department_id': self.department_id,
            'department_name': self.department.name if self.department else None,
            'status': self.status,
            'priority': self.priority,
            'ai_category': self.ai_category,
            'ai_sentiment': self.ai_sentiment,
            'ai_urgent': self.ai_urgent,
            'state': self.state,
            'district': self.district,
            'city': self.city,
            'escalation_level': self.escalation_level,
            'reopen_count': self.reopen_count,
            'sla_due_at': self.sla_due_at.isoformat() if self.sla_due_at else None,
            'is_delayed': self.status == 'Delayed',
            'citizen_rating': self.citizen_rating,
            'has_feedback': self.citizen_feedback is not None,
            'submitted_at': self.submitted_at.isoformat() if self.submitted_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'resolved_at': self.resolved_at.isoformat() if self.resolved_at else None
        }
        
        if include_details:
            data.update({
                'description': self.description,
                'has_evidence': self.evidence_path is not None,
                'assigned_to': self.assigned_to,
                'officer_name': self.assigned_officer.username if self.assigned_officer else None,
                'resolution_notes': self.resolution_notes,
                'resolution_hours': self.get_resolution_time(),
                'citizen_feedback': self.citizen_feedback,
                'location_lat': self.location_lat,
                'location_lng': self.location_lng
            })
        
        return data

    @staticmethod
    def apply_sla_escalations():
        """
        Apply SLA checks and auto-escalate overdue active complaints.
        Returns number of complaints auto-escalated.
        """
        now = datetime.utcnow()
        active = Complaint.query.filter(Complaint.status.in_(Complaint.ACTIVE_STATUSES)).all()
        escalated = []
        initialized_due_dates = False

        for complaint in active:
            before_due = complaint.sla_due_at
            complaint.initialize_sla_due()
            if before_due is None and complaint.sla_due_at is not None:
                initialized_due_dates = True
            if not complaint.sla_due_at or complaint.sla_due_at >= now:
                continue

            changed = False
            previous_status = complaint.status
            previous_level = complaint.escalation_level or 0

            if complaint.status != 'Delayed':
                complaint.status = 'Delayed'
                complaint.delayed_at = now
                changed = True

            if complaint.escalation_level < 2:
                complaint.escalation_level += 1
                changed = True

            assignee = complaint.assign_by_escalation_hierarchy()
            complaint.updated_at = now

            if changed:
                escalated.append((complaint, previous_status, previous_level, assignee))

        if not escalated and not initialized_due_dates:
            return 0

        db.session.commit()

        if not escalated:
            return 0

        for complaint, previous_status, previous_level, assignee in escalated:
            details = {
                'tracking_id': complaint.tracking_id,
                'previous_status': previous_status,
                'new_status': complaint.status,
                'previous_level': previous_level,
                'new_level': complaint.escalation_level,
                'assigned_to': assignee.username if assignee else None
            }
            AuditLog.create_entry(
                username='system',
                role='system',
                action='SLA_ESCALATED',
                details=json.dumps(details)
            )

        return len(escalated)
    
    @staticmethod
    def get_stats():
        """Get aggregate statistics for dashboard."""
        total = Complaint.query.count()
        pending = Complaint.query.filter_by(status='Pending').count()
        under_review = Complaint.query.filter_by(status='Under Review').count()
        action_taken = Complaint.query.filter_by(status='Action Taken').count()
        delayed = Complaint.query.filter_by(status='Delayed').count()
        reopened = Complaint.query.filter_by(status='Reopened').count()
        closed = Complaint.query.filter_by(status='Closed').count()
        high_priority = Complaint.query.filter_by(priority='High').count()

        closed_items = Complaint.query.filter_by(status='Closed').all()
        within_sla = 0
        for complaint in closed_items:
            if complaint.resolved_at and complaint.initialize_sla_due() and complaint.resolved_at <= complaint.sla_due_at:
                within_sla += 1
        sla_compliance = round((within_sla / len(closed_items) * 100), 2) if closed_items else 0
        
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
            'resolution_rate': round((closed / total * 100), 2) if total > 0 else 0
        }


class AuditLog(db.Model):
    """
    Immutable audit log with hash chaining for tamper evidence.
    Inspired by blockchain - each entry contains hash of previous entry.
    NO UPDATE OR DELETE routes exist for this table.
    """
    __tablename__ = 'audit_logs'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    username = db.Column(db.String(64), nullable=False, index=True)
    role = db.Column(db.String(30), nullable=False)
    action = db.Column(db.String(120), nullable=False, index=True)
    details = db.Column(db.Text, nullable=True)
    ip_address = db.Column(db.String(45), nullable=True)  # IPv6 compatible
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    previous_hash = db.Column(db.String(64), nullable=True)
    row_hash = db.Column(db.String(64), nullable=False, unique=True)
    
    def __repr__(self):
        return f'<AuditLog {self.username} {self.action} at {self.timestamp}>'
    
    def calculate_hash(self):
        """
        Calculate SHA-256 hash of this log entry.
        Includes previous hash for chain integrity.
        """
        data = {
            'user_id': self.user_id,
            'username': self.username,
            'role': self.role,
            'action': self.action,
            'details': self.details or '',
            'ip_address': self.ip_address or '',
            'timestamp': self.timestamp.isoformat() if self.timestamp else '',
            'previous_hash': self.previous_hash or ''
        }
        
        # Create deterministic string representation
        hash_string = json.dumps(data, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(hash_string.encode()).hexdigest()
    
    def verify_integrity(self):
        """Verify that stored hash matches calculated hash."""
        return self.row_hash == self.calculate_hash()

    @staticmethod
    def rebuild_chain(dry_run=False):
        """Rebuild the hash chain for all existing audit logs."""
        logs = AuditLog.query.order_by(AuditLog.id.asc()).all()
        if not logs:
            return {'total': 0, 'repaired': 0, 'dry_run': bool(dry_run)}

        repaired = 0
        previous_hash = None

        for log in logs:
            expected_previous = previous_hash
            current_previous = log.previous_hash

            # Apply canonical previous hash before calculating row hash.
            log.previous_hash = expected_previous
            expected_hash = log.calculate_hash()

            if current_previous != expected_previous or log.row_hash != expected_hash:
                repaired += 1
                if not dry_run:
                    log.previous_hash = expected_previous
                    log.row_hash = expected_hash

            previous_hash = expected_hash

        if dry_run or repaired == 0:
            if not dry_run:
                db.session.rollback()
            return {
                'total': len(logs),
                'repaired': repaired,
                'dry_run': bool(dry_run)
            }

        db.session.commit()
        return {
            'total': len(logs),
            'repaired': repaired,
            'dry_run': bool(dry_run)
        }
    
    @staticmethod
    def get_previous_hash():
        """Get hash of most recent audit log entry."""
        last_log = AuditLog.query.order_by(AuditLog.id.desc()).first()
        return last_log.row_hash if last_log else None
    
    @staticmethod
    def create_entry(username, role, action, details=None, user_id=None, ip_address=None):
        """
        Factory method to create a new audit log entry.
        Automatically handles hash chaining.
        """
        timestamp = datetime.utcnow()
        entry = AuditLog(
            user_id=user_id,
            username=username,
            role=role,
            action=action,
            details=details,
            ip_address=ip_address,
            timestamp=timestamp,
            previous_hash=AuditLog.get_previous_hash()
        )
        
        # Hash is deterministic from entry content + previous_hash.
        entry.row_hash = entry.calculate_hash()
        
        db.session.add(entry)
        db.session.commit()
        
        return entry
    
    def to_dict(self):
        """Serialize audit log to dictionary."""
        return {
            'id': self.id,
            'username': self.username,
            'role': self.role,
            'action': self.action,
            'details': self.details,
            'ip_address': self.ip_address,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'row_hash': self.row_hash[:16] + '...'  # Truncated for display
        }
