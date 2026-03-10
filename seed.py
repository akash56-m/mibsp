#!/usr/bin/env python3
"""
MIBSP Database Seeder
Creates demo data for development and testing.
"""
import os
import sys
import argparse
from datetime import datetime, timedelta
import random

# Add app to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app, db
from app.models import Department, Service, User, Complaint, AuditLog
from app.utils import generate_tracking_id


LOCATION_CATALOG = [
    {'state': 'Maharashtra', 'district': 'Mumbai Suburban', 'city': 'Mumbai', 'lat': 19.0760, 'lng': 72.8777},
    {'state': 'Maharashtra', 'district': 'Pune', 'city': 'Pune', 'lat': 18.5204, 'lng': 73.8567},
    {'state': 'Maharashtra', 'district': 'Nagpur', 'city': 'Nagpur', 'lat': 21.1458, 'lng': 79.0882},
    {'state': 'Maharashtra', 'district': 'Nashik', 'city': 'Nashik', 'lat': 19.9975, 'lng': 73.7898},
    {'state': 'Karnataka', 'district': 'Bengaluru Urban', 'city': 'Bengaluru', 'lat': 12.9716, 'lng': 77.5946},
    {'state': 'Karnataka', 'district': 'Mysuru', 'city': 'Mysuru', 'lat': 12.2958, 'lng': 76.6394},
    {'state': 'Karnataka', 'district': 'Dakshina Kannada', 'city': 'Mangaluru', 'lat': 12.9141, 'lng': 74.8560},
    {'state': 'Karnataka', 'district': 'Dharwad', 'city': 'Hubballi', 'lat': 15.3647, 'lng': 75.1240},
    {'state': 'Tamil Nadu', 'district': 'Chennai', 'city': 'Chennai', 'lat': 13.0827, 'lng': 80.2707},
    {'state': 'Telangana', 'district': 'Hyderabad', 'city': 'Hyderabad', 'lat': 17.3850, 'lng': 78.4867},
    {'state': 'Delhi', 'district': 'New Delhi', 'city': 'New Delhi', 'lat': 28.6139, 'lng': 77.2090},
    {'state': 'Gujarat', 'district': 'Ahmedabad', 'city': 'Ahmedabad', 'lat': 23.0225, 'lng': 72.5714},
    {'state': 'West Bengal', 'district': 'Kolkata', 'city': 'Kolkata', 'lat': 22.5726, 'lng': 88.3639},
    {'state': 'Uttar Pradesh', 'district': 'Lucknow', 'city': 'Lucknow', 'lat': 26.8467, 'lng': 80.9462},
    {'state': 'Rajasthan', 'district': 'Jaipur', 'city': 'Jaipur', 'lat': 26.9124, 'lng': 75.7873},
    {'state': 'Madhya Pradesh', 'district': 'Indore', 'city': 'Indore', 'lat': 22.7196, 'lng': 75.8577},
    {'state': 'Kerala', 'district': 'Ernakulam', 'city': 'Kochi', 'lat': 9.9312, 'lng': 76.2673},
    {'state': 'Bihar', 'district': 'Patna', 'city': 'Patna', 'lat': 25.5941, 'lng': 85.1376},
    {'state': 'Odisha', 'district': 'Khordha', 'city': 'Bhubaneswar', 'lat': 20.2961, 'lng': 85.8245},
    {'state': 'Punjab', 'district': 'Ludhiana', 'city': 'Ludhiana', 'lat': 30.9010, 'lng': 75.8573}
]


def random_nearby_coords(base_lat, base_lng):
    """Return slightly jittered coordinates around a city center."""
    lat = round(base_lat + random.uniform(-0.035, 0.035), 6)
    lng = round(base_lng + random.uniform(-0.035, 0.035), 6)
    return lat, lng


def seed_departments():
    """Create default departments."""
    departments_data = [
        {
            'name': 'Water Supply',
            'description': 'Water supply, distribution, and related services'
        },
        {
            'name': 'Roads & Infrastructure',
            'description': 'Road maintenance, street lights, and public infrastructure'
        },
        {
            'name': 'Public Health',
            'description': 'Public health services, sanitation, and hygiene'
        },
        {
            'name': 'Electricity',
            'description': 'Electricity supply and power-related services'
        },
        {
            'name': 'Sanitation',
            'description': 'Waste management and sanitation services'
        }
    ]
    
    departments = []
    for data in departments_data:
        dept = Department.query.filter_by(name=data['name']).first()
        if not dept:
            dept = Department(**data)
            db.session.add(dept)
            print(f"  Created department: {data['name']}")
        departments.append(dept)
    
    db.session.commit()
    return departments


def seed_services(departments):
    """Create services for each department."""
    services_data = {
        'Water Supply': [
            'Water Connection',
            'Water Quality Issue',
            'Pipeline Leakage',
            'Billing Complaint'
        ],
        'Roads & Infrastructure': [
            'Pothole Repair',
            'Street Light Issue',
            'Road Construction',
            'Drainage Problem'
        ],
        'Public Health': [
            'Mosquito Menace',
            'Garbage Collection',
            'Public Toilet Maintenance',
            'Health Violation'
        ],
        'Electricity': [
            'Power Outage',
            'Voltage Issue',
            'New Connection',
            'Meter Complaint'
        ],
        'Sanitation': [
            'Sewage Blockage',
            'Waste Collection',
            'Drain Cleaning',
            'Public Cleanliness'
        ]
    }
    
    services = []
    for dept in departments:
        dept_services = services_data.get(dept.name, [])
        for service_name in dept_services:
            existing = Service.query.filter_by(name=service_name, department_id=dept.id).first()
            if not existing:
                service = Service(
                    name=service_name,
                    department_id=dept.id,
                    description=f'{service_name} services'
                )
                db.session.add(service)
                print(f"  Created service: {service_name} ({dept.name})")
            services.append(existing or service)
    
    db.session.commit()
    return services


def seed_users(departments):
    """Create admin and officer users."""
    users_created = []
    
    # Create admin
    admin = User.query.filter_by(username='admin').first()
    if not admin:
        admin = User(
            username='admin',
            email='admin@mibsp.gov.in',
            role='admin',
            is_active=True
        )
        admin.set_password('Admin@1234')
        db.session.add(admin)
        users_created.append(('admin', 'Admin@1234', 'admin'))
        print("  Created user: admin (role: admin)")
    
    # Create officers
    officer_data = [
        ('officer_water', 'Water Supply'),
        ('officer_roads', 'Roads & Infrastructure'),
        ('officer_health', 'Public Health')
    ]
    
    for username, dept_name in officer_data:
        existing = User.query.filter_by(username=username).first()
        if not existing:
            dept = Department.query.filter_by(name=dept_name).first()
            officer = User(
                username=username,
                email=f'{username}@mibsp.gov.in',
                role='officer',
                department_id=dept.id if dept else None,
                is_active=True
            )
            officer.set_password('Officer@1234')
            db.session.add(officer)
            users_created.append((username, 'Officer@1234', 'officer'))
            print(f"  Created user: {username} (role: officer, dept: {dept_name})")
    
    db.session.commit()
    return users_created


def seed_complaints(departments, services, count=20):
    """Create sample complaints."""
    sample_descriptions = [
        "Water supply has been irregular for the past week. We are facing severe shortage.",
        "There is a large pothole on the main road causing accidents. Immediate repair needed.",
        "Garbage has not been collected for 3 days. Foul smell and health hazard.",
        "Street light not working for past 2 weeks. Area is dark and unsafe.",
        "Sewage overflow on the street. Request immediate cleaning.",
        "Power outage for 6 hours daily. Affecting work and daily life.",
        "Water quality is poor with bad smell and color. Not fit for consumption.",
        "Drainage blocked causing waterlogging during rains. Need urgent attention.",
        "Mosquito breeding in stagnant water. Risk of dengue outbreak.",
        "Illegal construction blocking road access. Please take action.",
        "Public toilet not maintained properly. Unhygienic conditions.",
        "Voltage fluctuations damaging electrical appliances. Need stabilizer.",
        "New water connection requested 2 months ago. No response yet.",
        "Road construction incomplete for past 3 months. Commuters suffering.",
        "Waste segregation not being followed. Need awareness and enforcement."
    ]
    
    statuses = ['Pending', 'Under Review', 'Action Taken', 'Delayed', 'Reopened', 'Closed']
    status_weights = [0.24, 0.24, 0.18, 0.12, 0.08, 0.14]
    
    officers = User.query.filter_by(role='officer').all()
    complaints_created = []
    
    for _ in range(count):
        dept = random.choice(departments)
        dept_services = [s for s in services if s.department_id == dept.id]
        service = random.choice(dept_services) if dept_services else None
        
        # Generate random dates within last 3 months
        days_ago = random.randint(1, 120)
        submitted_at = datetime.utcnow() - timedelta(days=days_ago)
        
        # Determine status
        status = random.choices(statuses, weights=status_weights)[0]
        
        location = random.choice(LOCATION_CATALOG)
        city_name = location['city']
        location_lat, location_lng = random_nearby_coords(location['lat'], location['lng'])

        # Create complaint
        complaint = Complaint(
            tracking_id=generate_tracking_id(),
            service_id=service.id if service else None,
            department_id=dept.id,
            description=random.choice(sample_descriptions),
            status=status,
            priority='High' if random.random() < 0.25 else 'Normal',
            escalation_level=random.choice([0, 0, 1, 2]) if status in ['Delayed', 'Reopened'] else 0,
            reopen_count=random.randint(1, 3) if status == 'Reopened' else 0,
            ai_sentiment=random.choice(['negative', 'neutral', 'negative', 'negative', 'positive']),
            ai_urgent=random.random() < 0.2,
            state=location['state'],
            district=location['district'],
            city=location['city'],
            location_lat=location_lat,
            location_lng=location_lng,
            submitted_at=submitted_at,
            updated_at=submitted_at
        )

        if complaint.ai_urgent:
            complaint.priority = 'High'

        if service:
            complaint.sla_due_at = submitted_at + timedelta(days=service.sla_days or 7)
        
        # Assign to officer if not pending
        if status != 'Pending' and officers:
            dept_officers = [o for o in officers if o.department_id == dept.id]
            if dept_officers:
                complaint.assigned_to = random.choice(dept_officers).id
        
        # Add resolution data if closed
        if status == 'Closed':
            complaint.resolved_at = submitted_at + timedelta(days=random.randint(3, 30))
            complaint.updated_at = complaint.resolved_at
            complaint.resolution_notes = "Complaint resolved. Appropriate action taken."
            if random.random() < 0.65:
                complaint.citizen_rating = random.randint(2, 5)
                complaint.citizen_feedback = random.choice([
                    'Issue resolved satisfactorily.',
                    'Partial resolution. Further monitoring needed.',
                    'Response was delayed but finally addressed.',
                    'Team was responsive once escalated.'
                ])
                complaint.feedback_submitted_at = complaint.resolved_at + timedelta(hours=random.randint(2, 72))

        if status == 'Delayed':
            complaint.delayed_at = submitted_at + timedelta(days=random.randint(7, 16))
            complaint.resolution_notes = f"Auto-marked delayed near {city_name} due to SLA breach."

        if status == 'Reopened':
            complaint.resolution_notes = (
                f"Complaint reopened by citizen in {city_name} after initial closure was unsatisfactory."
            )
        
        db.session.add(complaint)
        complaints_created.append(complaint)
        print(f"  Created complaint: {complaint.tracking_id} ({status})")
    
    db.session.commit()
    return complaints_created


def seed_audit_logs(users):
    """Create sample audit logs."""
    actions = [
        'LOGIN_SUCCESS',
        'COMPLAINT_SUBMITTED',
        'STATUS_UPDATE',
        'NOTES_ADDED',
        'COMPLAINT_ASSIGNED'
    ]
    
    for _ in range(30):
        user = random.choice(users) if users else None
        action = random.choice(actions)
        
        log = AuditLog.create_entry(
            user_id=user.id if user else None,
            username=user.username if user else 'anonymous',
            role=user.role if user else 'guest',
            action=action,
            details=f'Sample {action.lower()} entry',
            ip_address=f'192.168.1.{random.randint(1, 255)}'
        )
        print(f"  Created audit log: {action} by {log.username}")


def print_summary(users, complaints):
    """Print seeding summary."""
    print("\n" + "="*60)
    print("  SEEDING COMPLETE")
    print("="*60)
    print("\nCreated Users:")
    print("-" * 40)
    for username, password, role in users:
        print(f"  Username: {username}")
        print(f"  Password: {password}")
        print(f"  Role: {role}")
        print()
    
    print("\nSample Complaint Tracking IDs:")
    print("-" * 40)
    for complaint in complaints[:5]:
        print(f"  {complaint.tracking_id} - {complaint.status}")
    
    print("\n" + "="*60)
    print("  IMPORTANT: Change default passwords immediately!")
    print("="*60)


def _parse_complaint_count(raw_value):
    """Parse CLI complaint count with safe bounds."""
    if raw_value is None:
        return 20

    try:
        count = int(raw_value)
    except (TypeError, ValueError):
        raise ValueError('complaint count must be an integer.')

    if count < 1:
        raise ValueError('complaint count must be at least 1.')
    if count > 500:
        raise ValueError('complaint count must be 500 or below for seeding safety.')

    return count


def main():
    """Main seeding function."""
    parser = argparse.ArgumentParser(description='Seed initial MIBSP data for development/testing.')
    parser.add_argument(
        '--complaints',
        type=int,
        default=None,
        help='Number of demo complaints to generate. Takes precedence over --target-range.'
    )
    parser.add_argument(
        '--target-range',
        choices=['compact', 'medium', 'large'],
        default='compact',
        help='Preset complaint count: compact=20, medium=120, large=150.'
    )
    parser.add_argument(
        '--clear-existing-complaints',
        action='store_true',
        help='Delete existing complaints before generating new ones.'
    )
    parser.add_argument(
        '--seed-audit-logs',
        action='store_true',
        help='Create sample audit logs after seeding complaints.'
    )
    args = parser.parse_args()

    print("\n" + "="*60)
    print("  MIBSP Database Seeder")
    print("="*60 + "\n")

    target_count = {
        'compact': 20,
        'medium': 120,
        'large': 150
    }.get(args.target_range, 20)

    if args.complaints is not None:
        try:
            complaint_count = _parse_complaint_count(args.complaints)
        except ValueError as exc:
            raise SystemExit(f"Error: {exc}")
    else:
        complaint_count = target_count
    
    # Create app context
    env = os.environ.get('FLASK_ENV', 'development')
    app = create_app(env)
    
    with app.app_context():
        if args.clear_existing_complaints:
            deleted = Complaint.query.delete()
            db.session.commit()
            print(f"Cleared {deleted} existing complaints.")

        print("Creating departments...")
        departments = seed_departments()
        
        print("\nCreating services...")
        services = seed_services(departments)
        
        print("\nCreating users...")
        users = seed_users(departments)
        
        print("\nCreating complaints...")
        complaints = seed_complaints(departments, services, count=complaint_count)
        
        print("\nCreating audit logs...")
        if args.seed_audit_logs:
            all_users = User.query.all()
            seed_audit_logs(all_users)
        else:
            print("  Skipping audit logs. Use --seed-audit-logs to include.")
        
        print_summary(users, complaints)


if __name__ == '__main__':
    main()
