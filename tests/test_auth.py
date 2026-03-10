"""
MIBSP Authentication Tests
Tests for login, logout, and session management.
"""
import pytest
from app import create_app, db
from app.models import User


@pytest.fixture
def app():
    """Create application for testing."""
    app = create_app('testing')
    
    with app.app_context():
        db.create_all()
        # Create test user
        user = User(username='testuser', role='officer')
        user.set_password('testpass123')
        db.session.add(user)
        admin = User(username='adminuser', role='admin')
        admin.set_password('adminpass123')
        admin.email = 'admin@example.com'
        db.session.add(admin)
        db.session.commit()
        
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    """Create test client."""
    return app.test_client()


class TestLogin:
    """Tests for login functionality."""
    
    def test_login_page_loads(self, client):
        """Test login page loads."""
        response = client.get('/auth/login')
        assert response.status_code == 200
        assert b'Staff Login' in response.data
    
    def test_login_success(self, client):
        """Test successful login."""
        response = client.post('/auth/login', data={
            'username': 'testuser',
            'password': 'testpass123'
        }, follow_redirects=True)
        
        assert response.status_code == 200
        assert b'Welcome back' in response.data
    
    def test_login_wrong_password(self, client):
        """Test login with wrong password."""
        response = client.post('/auth/login', data={
            'username': 'testuser',
            'password': 'wrongpassword'
        })
        
        assert b'Invalid username or password' in response.data
    
    def test_login_nonexistent_user(self, client):
        """Test login with non-existent user."""
        response = client.post('/auth/login', data={
            'username': 'nonexistent',
            'password': 'somepassword'
        })
        
        assert b'Invalid username or password' in response.data

    def test_login_blocks_external_next_redirect(self, client):
        """Test login does not redirect to external domains."""
        response = client.post(
            '/auth/login?next=https://evil.example/steal',
            data={'username': 'testuser', 'password': 'testpass123'},
            follow_redirects=False
        )
        assert response.status_code == 302
        assert '/officer/dashboard' in response.headers.get('Location', '')

    def test_login_keeps_next_after_failed_attempt(self, client):
        """Next redirect target should remain in the form after validation errors."""
        response = client.post(
            '/auth/login?next=/admin/dashboard',
            data={
                'username': 'testuser',
                'password': 'wrong-pass'
            }
        )
        assert response.status_code == 200
        assert b'name="next" value="/admin/dashboard"' in response.data

    def test_login_locks_after_repeated_failures(self, client):
        """Test brute force lockout after repeated failed attempts."""
        for _ in range(5):
            client.post('/auth/login', data={
                'username': 'testuser',
                'password': 'wrongpassword'
            })

        response = client.post('/auth/login', data={
            'username': 'testuser',
            'password': 'testpass123'
        })
        assert b'temporarily locked' in response.data

    def test_admin_login_redirects_to_otp_when_enabled(self, client, app, monkeypatch):
        """Admin login should require OTP verification when feature is enabled."""
        app.config['ADMIN_EMAIL_2FA_ENABLED'] = True

        monkeypatch.setattr(
            'app.routes.auth.send_system_email',
            lambda subject, body, recipients: (True, None)
        )

        response = client.post('/auth/login', data={
            'username': 'adminuser',
            'password': 'adminpass123'
        }, follow_redirects=False)

        assert response.status_code == 302
        assert '/auth/verify-otp' in response.headers.get('Location', '')


class TestLogout:
    """Tests for logout functionality."""
    
    def test_logout(self, client):
        """Test logout functionality."""
        # Login first
        client.post('/auth/login', data={
            'username': 'testuser',
            'password': 'testpass123'
        })
        
        # Then logout
        response = client.post('/auth/logout', follow_redirects=True)
        assert response.status_code == 200
        assert b'logged out' in response.data

    def test_logout_get_not_allowed(self, client):
        """Logout should not be allowed via GET."""
        response = client.get('/auth/logout')
        assert response.status_code == 405


class TestSessionProtection:
    """Tests for session security."""
    
    def test_protected_route_redirects(self, client):
        """Test protected route redirects when not logged in."""
        response = client.get('/officer/dashboard', follow_redirects=True)
        assert b'Please log in' in response.data
    
    def test_session_persists(self, client):
        """Test session persists across requests."""
        # Login
        client.post('/auth/login', data={
            'username': 'testuser',
            'password': 'testpass123'
        })
        
        # Access protected page
        response = client.get('/officer/dashboard')
        assert response.status_code == 200

    def test_profile_page_loads_after_login(self, client):
        """Test profile page is available to logged-in users."""
        client.post('/auth/login', data={
            'username': 'testuser',
            'password': 'testpass123'
        })

        response = client.get('/auth/profile')
        assert response.status_code == 200
        assert b'My Profile' in response.data

    def test_admin_profile_shows_password_change_form(self, client):
        """Admin profile should show change password form."""
        client.post('/auth/login', data={
            'username': 'adminuser',
            'password': 'adminpass123'
        })

        response = client.get('/auth/profile')
        assert response.status_code == 200
        assert b'Change Admin Password' in response.data

    def test_admin_can_change_password_from_profile(self, client):
        """Admin should be able to change password with current password check."""
        client.post('/auth/login', data={
            'username': 'adminuser',
            'password': 'adminpass123'
        })

        response = client.post('/auth/profile/change-password', data={
            'current_password': 'adminpass123',
            'new_password': 'adminnew123',
            'confirm_password': 'adminnew123'
        }, follow_redirects=True)

        assert response.status_code == 200
        assert b'Admin password updated successfully' in response.data

        client.post('/auth/logout')
        relogin = client.post('/auth/login', data={
            'username': 'adminuser',
            'password': 'adminnew123'
        }, follow_redirects=True)
        assert relogin.status_code == 200
        assert b'Welcome back' in relogin.data

    def test_admin_password_change_rejects_wrong_current_password(self, client):
        """Admin password change should fail when current password is incorrect."""
        client.post('/auth/login', data={
            'username': 'adminuser',
            'password': 'adminpass123'
        })

        response = client.post('/auth/profile/change-password', data={
            'current_password': 'wrongpass123',
            'new_password': 'adminnew123',
            'confirm_password': 'adminnew123'
        }, follow_redirects=True)

        assert response.status_code == 200
        assert b'Current password is incorrect' in response.data

    def test_officer_cannot_use_admin_password_change_route(self, client):
        """Non-admin users should not be allowed to use admin password change route."""
        client.post('/auth/login', data={
            'username': 'testuser',
            'password': 'testpass123'
        })

        response = client.post('/auth/profile/change-password', data={
            'current_password': 'testpass123',
            'new_password': 'testnew123',
            'confirm_password': 'testnew123'
        }, follow_redirects=True)

        assert response.status_code == 200
        assert b'Only admins can change password from this section' in response.data
