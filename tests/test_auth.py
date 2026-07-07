"""认证模块测试"""
import pytest


class TestAuth:

    def test_login_missing_fields(self, client):
        """缺少用户名/密码返回 400"""
        resp = client.post('/api/v1/auth/login', json={})
        assert resp.status_code in (400, 401)

    def test_login_wrong_password(self, client, app):
        """错误密码登录失败"""
        from services.password_policy import hash_password
        from models.system import User, Role
        from extensions import db
        import json

        with app.app_context():
            role = Role.query.filter_by(name='admin').first()
            if not role:
                role = Role(name='admin', permissions=json.dumps(['*:*']))
                db.session.add(role)
                db.session.flush()
            if not User.query.filter_by(username='testuser').first():
                u = User(username='testuser', password_hash=hash_password('Right@123'), must_change_password=False)
                u.roles.append(role)
                db.session.add(u)
                db.session.commit()

        resp = client.post('/api/v1/auth/login', json={
            'username': 'testuser',
            'password': 'Wrong@123',
        })
        assert resp.status_code in (400, 401)

    def test_login_success(self, client, app):
        """正确密码登录成功并返回 token"""
        from services.password_policy import hash_password
        from models.system import User, Role
        from extensions import db
        import json

        with app.app_context():
            role = Role.query.filter_by(name='admin').first()
            if not role:
                role = Role(name='admin', permissions=json.dumps(['*:*']))
                db.session.add(role)
                db.session.flush()
            if not User.query.filter_by(username='loginuser').first():
                u = User(username='loginuser', password_hash=hash_password('Test@123456'), must_change_password=False)
                u.roles.append(role)
                db.session.add(u)
                db.session.commit()

        resp = client.post('/api/v1/auth/login', json={
            'username': 'loginuser',
            'password': 'Test@123456',
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'access_token' in data
        assert 'refresh_token' in data
