"""迁移链完整性测试（P1-3）

验证：
1. 迁移目录存在且非空
2. 迁移链单一 head（无分叉）
3. 所有迁移可被 alembic 解析（无语法错误）
4. down_revision 链完整可达 initial schema

生产环境 schema 变更必须用 flask db upgrade（硬约束），
测试用 db.create_all() 从最新 model 建表（SQLite 临时库），
迁移链完整性由本测试单独保障。
"""

import importlib.util
import os
from pathlib import Path

import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "versions"


def _load_migration_module(filepath):
    """动态加载迁移文件为 module，提取 revision/down_revision"""
    spec = importlib.util.spec_from_file_location(filepath.stem, filepath)
    module = importlib.util.module_from_spec(spec)
    # 迁移文件 import alembic.op，需 mock 避免 NameError
    import sys
    from unittest.mock import MagicMock

    sys.modules.setdefault("alembic", MagicMock())
    sys.modules.setdefault("alembic.op", MagicMock())
    spec.loader.exec_module(module)
    return module


class TestMigrations:
    """迁移链完整性测试"""

    def test_migrations_dir_exists(self):
        """迁移目录存在且非空"""
        assert MIGRATIONS_DIR.exists(), f"迁移目录不存在: {MIGRATIONS_DIR}"
        py_files = list(MIGRATIONS_DIR.glob("*.py"))
        py_files = [f for f in py_files if not f.name.startswith("__")]
        assert len(py_files) > 0, "迁移目录为空"

    def test_single_head_revision(self):
        """P1-3: 迁移链单一 head（无分叉）

        head = 所有 revision 中不出现在任何 down_revision 中的 revision
        （支持 alembic merge 迁移的元组 down_revision，如 h9i0j1k2l3m4）
        """
        revisions = {}
        down_revisions = set()
        for filepath in MIGRATIONS_DIR.glob("*.py"):
            if filepath.name.startswith("__"):
                continue
            try:
                module = _load_migration_module(filepath)
                rev = getattr(module, "revision", None)
                down = getattr(module, "down_revision", None)
                if rev:
                    revisions[rev] = filepath.name
                if down:
                    if isinstance(down, (tuple, list)):
                        down_revisions.update(down)
                    else:
                        down_revisions.add(down)
            except Exception as e:
                pytest.fail(f"迁移文件解析失败 {filepath.name}: {e}")

        # head = 在 revisions 中但不在 down_revisions 中
        heads = set(revisions.keys()) - down_revisions
        assert len(heads) == 1, (
            f"迁移链存在多个 head（分叉）: {heads}，"
            f"应合并为单一 head。revisions={set(revisions.keys())}, down_revisions={down_revisions}"
        )

    def test_down_revision_chain_reachable_to_base(self):
        """P1-3: down_revision 链从 head 可追溯到 base（None）"""
        revisions = {}
        for filepath in MIGRATIONS_DIR.glob("*.py"):
            if filepath.name.startswith("__"):
                continue
            try:
                module = _load_migration_module(filepath)
                rev = getattr(module, "revision", None)
                down = getattr(module, "down_revision", None)
                if rev:
                    revisions[rev] = down
            except Exception:
                continue

        # 找 head（元组 down_revision 展开为成员）
        all_revs = set(revisions.keys())
        all_downs = set()
        for d in revisions.values():
            if not d:
                continue
            if isinstance(d, (tuple, list)):
                all_downs.update(d)
            else:
                all_downs.add(d)
        heads = all_revs - all_downs
        if not heads:
            pytest.skip("无法确定 head，跳过链路检查")

        head = next(iter(heads))
        # 从 head 回溯到 None（merge 迁移的元组 down_revision 展开为多个父节点，用栈遍历）
        visited = set()
        stack = [head]
        while stack:
            current = stack.pop()
            if current is None or current in visited:
                continue
            visited.add(current)
            down = revisions.get(current)
            if isinstance(down, (tuple, list)):
                stack.extend(down)
            else:
                stack.append(down)

        # 所有 revision 应在回溯路径上（无游离迁移）
        assert visited == all_revs, f"存在游离迁移（不在 head 链路上）: {all_revs - visited}"
