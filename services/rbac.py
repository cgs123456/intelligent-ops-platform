"""RBAC 权限装饰器
P0-2: JWT 认证 + RBAC 权限校验。
- TESTING 模式下跳过认证（兼容测试）
- RBAC_ENABLED=0 时仅做 JWT 认证，不校验权限
- RBAC_ENABLED=1 时做 JWT 认证 + 权限校验
"""
import json
import logging
from functools import wraps
from flask import request, jsonify, g, current_app
from extensions import db
from models.system import User, Role

logger = logging.getLogger(__name__)


def get_current_user():
    """从 JWT payload（g.current_user）获取当前用户信息。
    需配合 @jwt_required / @require_permission 使用，或在公开接口中返回 None。
    """
    payload = getattr(g, 'current_user', None)
    if not payload:
        return None
    user = User.query.filter_by(
        username=payload.get('username'), is_active=True
    ).first()
    g.current_user_obj = user
    return user


def require_permission(permission):
    """权限校验装饰器：JWT 认证 + RBAC 权限检查。

    行为：
    - TESTING=True：完全跳过（兼容测试）
    - RBAC_ENABLED=False 且无 Authorization 头：放行（dev demo 模式）
    - RBAC_ENABLED=False 但带了 Authorization 头：验证 JWT，但不校验权限
    - RBAC_ENABLED=True：JWT 认证 + 权限校验
    """
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            # 测试模式跳过认证
            if current_app.config.get('TESTING'):
                return f(*args, **kwargs)

            from services.auth import AuthService
            auth_header = request.headers.get('Authorization', '')
            has_token = auth_header.startswith('Bearer ')

            # dev 模式（RBAC_ENABLED=False）且无 token：放行
            if not current_app.config.get('RBAC_ENABLED') and not has_token:
                return f(*args, **kwargs)

            # 有 token 必须验证
            if has_token:
                token = auth_header[7:]
                payload = AuthService.verify_token(token)
                if not payload:
                    return jsonify({'error': 'Token无效或已过期'}), 401
                g.current_user = payload

            # 第二步：RBAC 权限校验（仅 RBAC_ENABLED=True 时执行）
            if not current_app.config.get('RBAC_ENABLED'):
                return f(*args, **kwargs)
            user = get_current_user()
            if not user:
                return jsonify({'error': '用户不存在或已禁用'}), 401
            user_perms = _collect_permissions_safely(user)
            if permission not in user_perms and '*:*' not in user_perms:
                return jsonify({'error': f'无权限：{permission}'}), 403
            return f(*args, **kwargs)
        return wrapped
    return decorator


def _collect_permissions_safely(user):
    """收集用户权限。权限 JSON 解析异常显式记日志，不静默吞。"""
    perms = set()
    for role in user.roles:
        if role.permissions:
            try:
                perms.update(json.loads(role.permissions))
            except (json.JSONDecodeError, TypeError) as e:
                logger.error(
                    '角色权限 JSON 解析失败 role=%s user=%s err=%s，'
                    '该角色权限视为空集（拒绝授权更安全）',
                    role.name, user.username, e
                )
    return perms


def init_rbac_data(app):
    """初始化默认角色和 admin 用户（仅首次）

    P0 安全：admin 密码随机生成并打印到日志，标记 must_change_password=True，
    杜绝默认 admin123 弱口令。
    """
    with app.app_context():
        if Role.query.first():
            return
        roles = [
            Role(name='admin', permissions=json.dumps(['*:*']), description='管理员，全部权限'),
            Role(name='operator', permissions=json.dumps([
                'erp:read', 'erp:write', 'rpa:read', 'rpa:write',
                'fde:read', 'fde:run', 'aigc:read', 'aigc:review',
                'loop:read', 'loop:run', 'audit:read'
                # 注意：不含 loop:rollback 和 loop:reset（P0-9）
            ]), description='运营，可操作业务但不可重置/回滚'),
            Role(name='viewer', permissions=json.dumps([
                'erp:read', 'rpa:read', 'fde:read', 'aigc:read',
                'loop:read', 'audit:read'
            ]), description='查看者，只读'),
        ]
        db.session.add_all(roles)
        db.session.flush()

        # P0: admin 密码随机生成（不再硬编码 admin123）
        from services.auth import generate_random_password
        from services.password_policy import hash_password
        initial_password = generate_random_password(16)
        admin = User(
            username='admin',
            password_hash=hash_password(initial_password),
            display_name='管理员',
            must_change_password=True,
        )
        admin.roles.append(roles[0])
        db.session.add(admin)
        db.session.commit()

        # 打印初始密码到日志（仅首次启动）。注意：用 f-string 而非 %s，
        # 否则日志 formatter 会把字符串中的 %s 当作未替换占位符重复渲染
        border = '=' * 60
        msg = (
            f'\n{border}\n'
            f'首次启动：admin 账号已创建，初始密码：\n'
            f'    {initial_password}\n'
            f'请立即登录并修改密码！登录后系统会清除该标记。\n'
            f'此密码仅本次启动显示，请妥善保存。\n'
            f'{border}'
        )
        logger.warning(msg)
        # 同时输出到 stdout，方便 docker logs 查看
        print(msg)
