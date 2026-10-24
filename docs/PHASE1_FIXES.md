# Phase 1 修复记录（P0 止血）

> 对应 `OPTIMIZATION_REPORT.md` Phase 1 清单。全部修复均有测试或实机验证支撑。
> 验证环境：Windows 11 + Python 3.11.9 + PostgreSQL 16（docker）。
> 状态标记：✅ 已完成并验证 · 🔁 有回归测试 · 🖥️ 已实机验证

## 1. 业务正确性（资金/库存安全）

| # | 问题 | 修复 | 验证 |
|---|------|------|------|
| P0-2 | `create_return_order`/`transfer_stock` 负数数量绕过校验（`if qty > max` 对负数恒通过，`stock_qty += -N` 直接改库存） | `_validate_qty_price` 增加 bool 拒绝；新增 `_validate_positive_qty`，退货/调拨/MRP 下单统一走它（拒 0/负/小数/字符串/bool/None） | 🔁 tests/test_p0_fixes.py 第 8 节 |
| P0-3 | AIGC 审核改量只写 note，`suggested_qty` 从不生效，下游按原量下单 | `review_suggestion` 的 `final_qty` 覆盖 `suggested_qty`（正整数校验） | 🔁 test_review_final_qty_overrides_suggested_qty / test_review_approve_zero_final_qty_rejected |
| P0-4 | 财务对账 `close_batch` 以 \|discrepancy\|>0.01 一刀切拒关，税差场景永远无法关账 | auto_match 税容差匹配后已配对即允许关账；仅未配对且超容差才拒 | 🔁 test_close_batch_tax_tolerance_match_succeeds / test_close_batch_matched_pair_beyond_tolerance_fails |
| P0-5 | MRP release 用 `int(suggested_order_qty)`，0/负量直接生成采购单 | release 前走 `_validate_positive_qty`，组件成本非零校验 | 🔁 test_p0_fixes.py |
| P0-6 | `mfa_disable` 仅凭 token 即可关闭 MFA（被盗 token 可立即撤掉第二因素） | 关闭需 password + 当前 TOTP 双重再认证 | 🔁 test_p0_fixes.py 第 9 节 TestMfaDisableRequiresReauth |
| P0-8 | `mfa_setup` 未验证任何 TOTP 码就直接启用 MFA（扫码失败即被锁死） | 绑定流程改为 verify-then-enable：先发 secret，验证通过才置 `mfa_enabled=True` | 🔁（随 auth 测试） |

## 2. 可运行性（此前装不上 / 起不来 / 部署必炸）

| # | 问题 | 修复 | 验证 |
|---|------|------|------|
| P0-9 | `pip install -r requirements.txt` 在干净环境必然失败：OTel 全系 prerelease 版本，默认解析拒绝（另叠加 Windows GBK 读中文注释报错） | OTel 精确 pin（0.66b1 / 1.45.1 等），显式 pin 允许 prerelease；`--dry-run --ignore-installed` 无 `--pre` 全量解析通过 | 🖥️ |
| P0-7 | `celery beat` 启动即崩：beat_schedule 用裸 cron 字符串，`celery.schedules.maybe_schedule` 对 str 抛 `AttributeError: 'str' object has no attribute 'app'` | 全部改 `crontab(...)` 对象 | 🔁 test_beat_schedule_all_crontab（5 条 schedule 类型断言） |
| 驱动 | SQLAlchemy 2.x `postgresql://` 默认加载 psycopg(3) 不再回退，requirements 装的是 psycopg2-binary → PG 环境（含 CD 的 `flask db upgrade`）启动即 `ModuleNotFoundError: psycopg` | requirements 改 `psycopg[binary]==3.3.6` | 🖥️ 本地 PG 16 容器实测 |
| P0-10 | **迁移链没有建表迁移**：11 个迁移全是"对已存在库的增量补丁"，空库（生产首次部署）`flask db upgrade` 第一步就 UndefinedTable；dev"能用"纯属 `create_all()` 抢跑掩盖 | squash 为单一 baseline `b00000000001`：其余表由当前模型 `metadata.create_all`（模型即真相）；audit_log 在 PG 按 P2-14 分区策略建（月分区+默认分区，索引与模型完全对齐）；删除 10 个增量迁移；顺手补全 `models/__init__.py` 缺失的 finance/bom/TokenBlacklist 导出（这些类此前未注册进 metadata，baseline 会漏表） | 🖥️ 空库 PG 实测：53 表 + audit_log 分区表 + 2026_10~2027_03 月分区 + default 分区，`flask db check` 零真实差异（仅 alembic 对分区子表的固有 remove_table 误报）；dev 语义（create_all 抢跑）下 upgrade/check 亦通过 |

## 3. 部署链（CD/回滚/告警）

| # | 问题 | 修复 | 验证 |
|---|------|------|------|
| CD-1 | main 分支 push 直接部署，测试/安全扫描失败的同一次提交照样上线 | cd.yml 触发改 `workflow_run: [CI completed]`，build/deploy 加 `conclusion == 'success'` 门禁；tag/手动触发不受影响 | YAML 解析断言 |
| CD-2 | alertmanager entrypoint 用 envsubst 渲染 `${VAR:default}` 模板——envsubst 不支持该语法，变量**永远**不被替换，告警全部发往非法 URL | 改 POSIX 参数展开 + sed（`&`/`\` 转义），渲染后自检无 `${ALERTMANAGER_` 残留；模板改纯 `${VAR}` 占位 | 🖥️ 真实脚本实跑：设置/未设置变量两场景 + 含 `&` 的 URL 均正确 |
| CD-3 | rollback.sh `up -d --no-deps` 不指定服务 → 回滚连 db/redis/nginx 一起重建；`docker exec ops_nginx` 容器名错（实际服务名 nginx）；不回写 .env → 下轮部署版本错乱 | 显式服务列表（web celery-worker celery-beat）+ 回写 `.env` IMAGE_TAG（与 CD 同源）+ nginx 容器名双兜底 | bash -n + 逻辑比对 CD |
| CI-migrate | 迁移链从未在 PG 上验证过（SQLite 测试跳过 PG 专属 DDL） | ci.yml 新增 `migrate` job：PG16 service container，生产语义空库跑 `flask db upgrade` + current==(head) 断言 + 表数量/分区抽查；纳入 build 前置 | 🖥️ 本地同流程实测通过（该 job 正是发现 P0-10 的手段） |

## 4. 测试体系

- Celery 9 个失败测试密闭化重写：`tasks_without_celery` fixture（reload 模式）测 `_NullTask` 分支；celery 可用时反向断言任务真绑定 + beat schedule 全 `crontab`。
- 全量 **507 passed / 2 skipped / 0 failed**（32.6s；2 skip 为 celery 可用世界的正确跳过）。

## 遗留已知边界（有意为之）

1. `flask db check` 在 PG 分区库上会报 audit_log 分区子表 remove_table——alembic autogenerate 不理解声明式分区，属工具误报；CI 用 current==(head) + 对象抽查替代（ci.yml 内有注释）。
2. dev（FLASK_ENV=development）PG 库的 audit_log 是普通表（create_all 抢跑，baseline 幂等跳过）；分区仅生产语义生效。
3. 迁移历史已 squash：无存量部署库（CD 此前从未成功运行），无兼容义务。此后 schema 变更一律新增迁移文件。
