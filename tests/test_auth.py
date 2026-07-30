"""认证模块测试"""

import pytest


class TestAuth:

    def test_login_missing_fields(self, client):
        """缺少用户名/密码返回 400"""
        resp = client.post("/api/v1/auth/login", json={})
        assert resp.status_code in (400, 401)

    def test_login_wrong_password(self, client, app):
        """错误密码登录失败"""
        import json

        from extensions import db
        from models.system import Role, User
        from services.password_policy import hash_password

        with app.app_context():
            role = Role.query.filter_by(name="admin").first()
            if not role:
                role = Role(name="admin", permissions=json.dumps(["*:*"]))
                db.session.add(role)
                db.session.flush()
            if not User.query.filter_by(username="testuser").first():
                u = User(username="testuser", password_hash=hash_password("Right@123"), must_change_password=False)
                u.roles.append(role)
                db.session.add(u)
                db.session.commit()

        resp = client.post(
            "/api/v1/auth/login",
            json={
                "username": "testuser",
                "password": "Wrong@123",
            },
        )
        assert resp.status_code in (400, 401)

    def test_login_success(self, client, app):
        """正确密码登录成功并返回 token"""
        import json

        from extensions import db
        from models.system import Role, User
        from services.password_policy import hash_password

        with app.app_context():
            role = Role.query.filter_by(name="admin").first()
            if not role:
                role = Role(name="admin", permissions=json.dumps(["*:*"]))
                db.session.add(role)
                db.session.flush()
            if not User.query.filter_by(username="loginuser").first():
                u = User(username="loginuser", password_hash=hash_password("Test@123456"), must_change_password=False)
                u.roles.append(role)
                db.session.add(u)
                db.session.commit()

        resp = client.post(
            "/api/v1/auth/login",
            json={
                "username": "loginuser",
                "password": "Test@123456",
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "access_token" in data
        assert "refresh_token" in data

    def test_login_sets_httponly_cookie(self, client, app):
        """P1-12: 登录成功后通过 Set-Cookie 下发 HttpOnly token cookie"""
        import json

        from extensions import db
        from models.system import Role, User
        from services.password_policy import hash_password

        with app.app_context():
            role = Role.query.filter_by(name="admin").first()
            if not role:
                role = Role(name="admin", permissions=json.dumps(["*:*"]))
                db.session.add(role)
                db.session.flush()
            if not User.query.filter_by(username="cookieuser").first():
                u = User(username="cookieuser", password_hash=hash_password("Test@123456"), must_change_password=False)
                u.roles.append(role)
                db.session.add(u)
                db.session.commit()

        resp = client.post(
            "/api/v1/auth/login",
            json={"username": "cookieuser", "password": "Test@123456"},
        )
        assert resp.status_code == 200
        # 验证 Set-Cookie 头存在且为 HttpOnly
        set_cookie_headers = resp.headers.getlist("Set-Cookie")
        cookie_str = "; ".join(set_cookie_headers)
        assert "access_token=" in cookie_str
        assert "HttpOnly" in cookie_str
        assert "refresh_token=" in cookie_str
        assert "SameSite=Strict" in cookie_str
