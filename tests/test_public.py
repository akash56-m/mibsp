"""
MIBSP Public Routes Tests
Tests for citizen-facing functionality.
"""
import pytest
from datetime import datetime, timedelta
from app import create_app, db
from app.models import Department, Service, Complaint


@pytest.fixture
def app():
    """Create application for testing."""
    app = create_app('testing')
    
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    """Create test client."""
    return app.test_client()


@pytest.fixture
def sample_data(app):
    """Create sample data for tests."""
    with app.app_context():
        dept = Department(name='Test Department', description='Test')
        db.session.add(dept)
        db.session.commit()
        
        service = Service(name='Test Service', department_id=dept.id)
        db.session.add(service)
        db.session.commit()
        
        return {'department_id': dept.id, 'service_id': service.id}


class TestHomepage:
    """Tests for homepage."""
    
    def test_homepage_loads(self, client):
        """Test homepage loads successfully."""
        response = client.get('/')
        assert response.status_code == 200
        assert b'MIBSP' in response.data
    
    def test_homepage_shows_stats(self, client):
        """Test homepage shows statistics."""
        response = client.get('/')
        assert response.status_code == 200
        assert b'Total Complaints' in response.data

    def test_homepage_shows_ai_chatbot(self, client):
        """Homepage should include AI helper chatbot section."""
        response = client.get('/')
        assert response.status_code == 200
        assert b'Need Help Using The Portal?' in response.data
        assert b'id="homeAiForm"' in response.data

    def test_about_page_loads(self, client):
        """Test about page loads successfully."""
        response = client.get('/about')
        assert response.status_code == 200
        assert b'About MIBSP' in response.data

    def test_how_it_works_page_loads(self, client):
        """Test how-it-works page loads successfully."""
        response = client.get('/how-it-works')
        assert response.status_code == 200
        assert b'How The Portal Works' in response.data

    def test_favicon_route_exists(self, client):
        """Favicon route should resolve through static redirect."""
        response = client.get('/favicon.ico')
        assert response.status_code in (200, 301, 302, 308)


class TestComplaintSubmission:
    """Tests for complaint submission."""
    
    def test_submit_page_loads(self, client):
        """Test submit page loads."""
        response = client.get('/submit')
        assert response.status_code == 200
        assert b'Submit Complaint' in response.data
    
    def test_submit_complaint_success(self, client, sample_data):
        """Test successful complaint submission."""
        response = client.post('/submit', data={
            'department_id': sample_data['department_id'],
            'service_id': sample_data['service_id'],
            'description': 'This is a test complaint with sufficient length for validation.',
            'state': 'Maharashtra',
            'district': 'Mumbai Suburban',
            'city': 'Mumbai'
        }, follow_redirects=True)
        
        assert response.status_code == 200
        assert b'Complaint submitted successfully' in response.data
        
        # Verify complaint was created
        complaint = Complaint.query.first()
        assert complaint is not None
        assert complaint.tracking_id.startswith('MIB')
        assert complaint.state == 'Maharashtra'
        assert complaint.district == 'Mumbai Suburban'
        assert complaint.city == 'Mumbai'
    
    def test_submit_complaint_validation(self, client, sample_data):
        """Test complaint submission validation."""
        # Too short description
        response = client.post('/submit', data={
            'department_id': sample_data['department_id'],
            'service_id': sample_data['service_id'],
            'description': 'Short'
        })
        
        assert b'Description must be at least 50 characters' in response.data


class TestComplaintTracking:
    """Tests for complaint tracking."""
    
    def test_track_page_loads(self, client):
        """Test track page loads."""
        response = client.get('/track')
        assert response.status_code == 200
        assert b'Track Your Complaint' in response.data
    
    def test_track_complaint_found(self, client, sample_data):
        """Test tracking a valid complaint."""
        # Create a complaint first
        complaint = Complaint(
            tracking_id='MIBTEST1234',
            service_id=sample_data['service_id'],
            department_id=sample_data['department_id'],
            description='Test complaint for tracking'
        )
        db.session.add(complaint)
        db.session.commit()
        
        response = client.post('/track', data={
            'tracking_id': 'MIBTEST1234'
        })
        
        assert response.status_code == 200
        assert b'MIBTEST1234' in response.data
    
    def test_track_complaint_not_found(self, client):
        """Test tracking non-existent complaint."""
        response = client.post('/track', data={
            'tracking_id': 'MIBINVALID1'
        })
        
        assert b'Complaint not found' in response.data

    def test_reopen_closed_complaint(self, client, sample_data):
        """Citizen can reopen a closed complaint."""
        complaint = Complaint(
            tracking_id='MIBCLOSE123',
            service_id=sample_data['service_id'],
            department_id=sample_data['department_id'],
            description='This complaint was marked closed but issue still persists.',
            status='Closed',
            resolved_at=datetime.utcnow()
        )
        db.session.add(complaint)
        db.session.commit()

        response = client.post(
            '/complaint/MIBCLOSE123/reopen',
            data={'reopen_reason': 'Issue still unresolved and requires further action.'},
            follow_redirects=True
        )

        assert response.status_code == 200
        updated = Complaint.query.filter_by(tracking_id='MIBCLOSE123').first()
        assert updated.status == 'Reopened'
        assert updated.reopen_count == 1

    def test_feedback_submission_for_closed_complaint(self, client, sample_data):
        """Citizen can submit anonymous feedback on closed complaint."""
        complaint = Complaint(
            tracking_id='MIBFEED1234',
            service_id=sample_data['service_id'],
            department_id=sample_data['department_id'],
            description='Closed complaint for feedback test.',
            status='Closed',
            resolved_at=datetime.utcnow()
        )
        db.session.add(complaint)
        db.session.commit()

        response = client.post(
            '/complaint/MIBFEED1234/feedback',
            data={'rating': '4', 'feedback': 'Handled reasonably well.'},
            follow_redirects=True
        )
        assert response.status_code == 200

        updated = Complaint.query.filter_by(tracking_id='MIBFEED1234').first()
        assert updated.citizen_rating == 4
        assert updated.citizen_feedback == 'Handled reasonably well.'


class TestPublicDashboard:
    """Tests for public dashboard."""
    
    def test_dashboard_loads(self, client):
        """Test dashboard loads successfully."""
        response = client.get('/dashboard')
        assert response.status_code == 200
        assert b'Public Transparency Dashboard' in response.data

    def test_geo_heatmap_page_loads(self, client):
        """Test geo heatmap page loads successfully."""
        response = client.get('/geo-heatmap')
        assert response.status_code == 200
        assert b'Complaint Geo Heatmap' in response.data
        assert b'Reset Filters' in response.data
        assert b'All States' in response.data
        assert b'All Districts' in response.data
        assert b'All Cities' in response.data


class TestAPIEndpoints:
    """Tests for public API endpoints."""
    
    def test_health_check(self, client):
        """Test health check endpoint."""
        response = client.get('/health')
        assert response.status_code == 200
        assert b'healthy' in response.data
    
    def test_get_stats(self, client):
        """Test stats API endpoint."""
        response = client.get('/api/stats')
        assert response.status_code == 200
        data = response.get_json()
        assert 'total' in data
        assert 'pending' in data

    def test_dashboard_overview_api(self, client, sample_data):
        """Dashboard overview API should return aggregate payload for live filters."""
        complaint = Complaint(
            tracking_id='MIBOVR12345',
            service_id=sample_data['service_id'],
            department_id=sample_data['department_id'],
            description='Dashboard overview sample complaint for payload checks.',
            status='Pending'
        )
        db.session.add(complaint)
        db.session.commit()

        response = client.get('/api/dashboard/overview')
        assert response.status_code == 200
        payload = response.get_json()
        assert 'stats' in payload
        assert 'dept_stats' in payload
        assert 'recent_complaints' in payload

    def test_dashboard_overview_rejects_invalid_month(self, client):
        """Dashboard overview should reject malformed month filters."""
        response = client.get('/api/dashboard/overview?from_month=2026-13')
        assert response.status_code == 400
    
    def test_get_services(self, client, sample_data):
        """Test services API endpoint."""
        dept_id = sample_data['department_id']
        response = client.get(f'/api/services/{dept_id}')
        assert response.status_code == 200
        data = response.get_json()
        assert isinstance(data, list)

    def test_chart_dept_supports_department_filter(self, client, sample_data):
        """Department chart should support department-level filtering."""
        response = client.get(f'/api/chart/dept?department_id={sample_data["department_id"]}')
        assert response.status_code == 200
        payload = response.get_json()
        assert isinstance(payload.get('labels'), list)
        assert isinstance(payload.get('data'), list)
        assert len(payload['labels']) == 1

    def test_ai_assist_requires_json(self, client):
        """AI endpoint should require JSON payloads."""
        response = client.post('/api/ai/assist', data={'message': 'help me'})
        assert response.status_code == 400

    def test_ai_assist_unconfigured_returns_fallback(self, client):
        """AI endpoint should return local fallback when API key is not configured."""
        response = client.post('/api/ai/assist', json={'message': 'help me draft complaint'})
        assert response.status_code == 200
        data = response.get_json()
        assert data.get('fallback') is True
        assert isinstance(data.get('reply'), str)
        assert data.get('reply')

    def test_ai_assist_homepage_mode_unconfigured_returns_fallback(self, client, app):
        """Homepage mode should return local fallback when API key is not configured."""
        app.config['AI_RATE_MIN_INTERVAL_SECONDS'] = 0
        response = client.post('/api/ai/assist', json={
            'assistant': 'homepage',
            'message': 'How do I submit a complaint?'
        })
        assert response.status_code == 200
        data = response.get_json()
        assert data.get('fallback') is True
        assert isinstance(data.get('reply'), str)
        assert data.get('reply')

    def test_ai_classify_endpoint_returns_prediction(self, client):
        """AI classify endpoint should return category and service suggestion payload."""
        dept = Department(name='Water Supply', description='Water services')
        db.session.add(dept)
        db.session.commit()

        service = Service(
            name='Water Leakage Repair',
            description='Leakage and broken pipeline repair',
            department_id=dept.id
        )
        db.session.add(service)
        db.session.commit()

        response = client.post('/api/ai/classify', json={
            'description': 'There is a serious water leakage from the municipal pipeline for two days.'
        })
        assert response.status_code == 200
        payload = response.get_json()
        assert payload.get('priority') in ['Normal', 'High']
        assert 'sentiment' in payload
        assert payload.get('service_name') == 'Water Leakage Repair'
        assert payload.get('department_name') == 'Water Supply'

    def test_ai_classify_requires_minimum_description(self, client):
        """AI classify endpoint should reject very short descriptions."""
        response = client.post('/api/ai/classify', json={'description': 'too short'})
        assert response.status_code == 400

    def test_sla_escalation_runs_on_stats_api(self, client, sample_data):
        """Stats API should trigger SLA escalation for overdue complaints."""
        complaint = Complaint(
            tracking_id='MIBSLA12345',
            service_id=sample_data['service_id'],
            department_id=sample_data['department_id'],
            description='Overdue complaint should auto-escalate.',
            status='Pending',
            sla_due_at=datetime.utcnow() - timedelta(days=1)
        )
        db.session.add(complaint)
        db.session.commit()

        response = client.get('/api/stats')
        assert response.status_code == 200

        updated = Complaint.query.filter_by(tracking_id='MIBSLA12345').first()
        assert updated.status == 'Delayed'
        assert updated.escalation_level >= 1

    def test_public_csv_export(self, client):
        """Public monthly CSV export endpoint should return CSV."""
        response = client.get('/api/public/export/monthly.csv')
        assert response.status_code == 200
        assert 'text/csv' in response.content_type

    def test_geo_heatmap_api_returns_points(self, client, sample_data):
        """Geo heatmap API should include geotagged complaints."""
        complaint = Complaint(
            tracking_id='MIBGEO12345',
            service_id=sample_data['service_id'],
            department_id=sample_data['department_id'],
            description='Geo complaint sample for map tests.',
            state='Maharashtra',
            district='Mumbai Suburban',
            city='Mumbai',
            location_lat=12.9716,
            location_lng=77.5946
        )
        db.session.add(complaint)
        db.session.commit()

        response = client.get('/api/geo/heatmap')
        assert response.status_code == 200
        data = response.get_json()
        assert isinstance(data, list)
        record = next(item for item in data if item.get('tracking_id') == 'MIBGEO12345')
        assert record['state'] == 'Maharashtra'
        assert record['district'] == 'Mumbai Suburban'
        assert record['city'] == 'Mumbai'
