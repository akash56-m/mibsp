#!/usr/bin/env python3
"""
Production-safe bootstrap script.

Creates required database tables and baseline reference data when missing.
Designed for deploy targets that don't maintain migration scripts.
"""
import os
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app import create_app, db
from app.models import Department, Service, User
from sqlalchemy.exc import OperationalError


BASELINE_DATA = [
    {
        "department": "Water Supply",
        "description": "Water supply, distribution, and related services",
        "services": [
            "Water Connection",
            "Water Quality Issue",
            "Pipeline Leakage",
            "Billing Complaint",
        ],
    },
    {
        "department": "Roads & Infrastructure",
        "description": "Road maintenance, street lights, and public infrastructure",
        "services": [
            "Pothole Repair",
            "Street Light Issue",
            "Road Construction",
            "Drainage Problem",
        ],
    },
    {
        "department": "Public Health",
        "description": "Public health services, sanitation, and hygiene",
        "services": [
            "Mosquito Menace",
            "Garbage Collection",
            "Public Toilet Maintenance",
            "Health Violation",
        ],
    },
    {
        "department": "Electricity",
        "description": "Electricity supply and power-related services",
        "services": [
            "Power Outage",
            "Voltage Issue",
            "New Connection",
            "Meter Complaint",
        ],
    },
    {
        "department": "Sanitation",
        "description": "Waste management and sanitation services",
        "services": [
            "Sewage Blockage",
            "Waste Collection",
            "Drain Cleaning",
            "Public Cleanliness",
        ],
    },
]


def ensure_lookup_data():
    for item in BASELINE_DATA:
        department = Department.query.filter_by(name=item["department"]).first()
        if not department:
            department = Department(
                name=item["department"],
                description=item["description"]
            )
            db.session.add(department)
            db.session.flush()

        existing_services = {
            service.name: service
            for service in Service.query.filter_by(department_id=department.id).all()
        }

        for service_name in item["services"]:
            if service_name not in existing_services:
                db.session.add(
                    Service(
                        name=service_name,
                        department_id=department.id,
                        description=f"{service_name} services"
                    )
                )


def ensure_admin():
    username = os.environ.get("DEFAULT_ADMIN_USERNAME", "admin")
    email = os.environ.get("DEFAULT_ADMIN_EMAIL", "admin@mibsp.gov.in")
    password = os.environ.get("DEFAULT_ADMIN_PASSWORD", "Admin@1234")

    admin = User.query.filter_by(username=username).first()
    if admin is not None:
        return

    admin = User(
        username=username,
        email=email,
        role="admin",
        is_active=True
    )
    admin.set_password(password)
    db.session.add(admin)


def main():
    env = os.environ.get("FLASK_ENV", "production")
    app = create_app(env)
    max_retries = int(os.environ.get("BOOTSTRAP_DB_RETRIES", "8"))
    retry_delay = float(os.environ.get("BOOTSTRAP_DB_RETRY_DELAY", "2"))

    with app.app_context():
        for attempt in range(1, max_retries + 1):
            try:
                db.create_all()
                ensure_lookup_data()
                ensure_admin()
                db.session.commit()
                break
            except OperationalError:
                if attempt >= max_retries:
                    raise
                print(f"[boot] Database not ready (attempt {attempt}/{max_retries}); retrying in {retry_delay}s")
                time.sleep(retry_delay)
                continue

        print("[boot] DB tables ensured")
        print("[boot] Baseline departments/services ensured")
        print("[boot] Admin account ensured")


if __name__ == "__main__":
    main()
