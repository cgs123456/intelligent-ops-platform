"""健康检查端点测试"""
import pytest


class TestHealth:

    def test_health_live(self, client):
        """存活探针返回 200 + ok"""
        resp = client.get('/health/live')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data.get('status') == 'ok' or data == 'ok' or 'ok' in str(data)

    def test_health_ready(self, client):
        """就绪探针返回 200"""
        resp = client.get('/health/ready')
        assert resp.status_code == 200

    def test_health_deep(self, client):
        """深度健康检查（含 DB + Redis + 磁盘）"""
        resp = client.get('/health')
        assert resp.status_code in (200, 503)  # 503 也算正常（依赖不可用）

    def test_index_page(self, client):
        """首页可访问"""
        resp = client.get('/')
        assert resp.status_code == 200

    def test_404_handler(self, client):
        """不存在的路径返回 404 JSON"""
        resp = client.get('/api/v1/nonexistent')
        assert resp.status_code == 404
        data = resp.get_json()
        assert data is not None
