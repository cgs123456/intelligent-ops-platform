# P3 业务功能增强（第一批）

> 对应 OPTIMIZATION_REPORT.md Phase 3 中的业务演进项，参照 stockpyl / Odoo orderpoint /
> ERPNext / 主流补货 Agent 项目的共识功能模式，结合本项目数据基础筛选后落地。
> 本批四项：F1 安全库存重算、F2 SKU ABC/XYZ 分级、F3 高金额审批、F4 会计期间锁定。

## F1 安全库存重算（统计安全库存）

**痛点**：`Product.safety_stock` 是静态手工字段，与需求波动、供应商交期脱钩；
补货建议的"安全库存"口径无依据。

**实现**：
- `services/forecast.py` 新增 `safety_stock_recommendation(daily_series, lead_days,
  service_level, window_days)`：`SS = z × σ_日需求 × √提前期`。
  - z 表支持 0.90 / 0.95 / 0.98 / 0.99 四档服务水位（z=1.2816 / 1.6449 / 2.0537 / 2.3263），
    其他水位抛 `ValueError`（避免静默外推）。
  - σ 取窗口内**含零日**的日销量标准差（缺销日也是真实需求实现）。
  - `lead_days <= 0` 按 1 处理。
- `ERPService.recalc_safety_stock(product_id, service_level=0.95, window_days=90,
  apply=False, actor)`：σ 取 DWS 日销序列；提前期取最优活跃报价供应商的
  `lead_days`（与 ADS 补货同口径），无报价回退 5 天；`apply=True` 写回
  `Product.safety_stock` 并记审计。
- 路由：`POST /api/v1/erp/products/<id>/safety-stock-recalc`（`erp:write`）。
  Body 可选 `service_level` / `window_days` / `apply`（默认 false 仅预览）。

**不做 EOQ**：经济订货批量需要年需求 D、单次订货成本 S、持有成本 H 三个参数，
本项目现有数据基础（无订货成本、持有成本字段）不具备，硬凑参数只会产出
伪精确数字。待字段补齐后再评估。

## F2 SKU ABC/XYZ 分级

**痛点**：补货建议对所有 SKU 一视同仁，无法按"价值 × 波动"差异化策略
（A 类缺货代价高需高水位，C 类 Z 波动大不宜多备）。

**实现**：
- `models/warehouse.py` `AdsReplenishmentSuggest` 新增四列：
  `abc_class` / `xyz_class` / `revenue_share_90d` / `cv_90d`。
- `WarehouseService._etl_ads` 注入分级计算（每次 ETL 刷新，窗口
  `FDE_CLASSIFY_WINDOW_DAYS` 默认 90 天）：
  - **ABC**：按窗口销售额帕累托排序，累计占比 ≤80% 为 A、≤95% 为 B、其余 C；
    销售额第一名强制 A（垄断型 SKU 结构也保证有 A 层）。
  - **XYZ**：日销量变异系数 `cv = σ/μ`，<0.5 为 X（稳定）、<1.0 为 Y、其余 Z。
  - **口径决策**：σ 与 μ 按有 DWS 记录的天数同口径计算。DWS 只在有销售日落行，
    零销日缺口无法与"数据未铺满窗口"区分；按窗口天数硬除会把历史不足 90 天的
    稳定 SKU 误判为 Z（过度备货）。代价是对真正的间歇性需求低估波动——
    两害相权取轻，已在代码注释中记录。
- 路由：`GET /api/v1/fde/sku-classification`（`fde:read`），支持
  `dt` / `abc` / `xyz` 过滤，按 `revenue_share_90d` 倒序最多 500 条。

## F3 高金额补货建议审批

**痛点**：审核权限只有一档 `aigc:review`，巨额建议与普通建议同权限可批，
缺乏金额分级管控（参照 ERP 采购审批流的金额阈值模式）。

**实现**：
- `config.py` 新增 `SUGGESTION_HIGH_VALUE_AMOUNT`（默认 50000，可环境变量覆盖）。
- `AIGCService.review_suggestion` 新增 `actor_permissions` 参数：
  - 金额 = `数量 × 单价`；单价优先取该产品**活跃供应商报价最低价**（补货按最优
    报价估算），无报价回退 `Product.avg_cost`（移动加权成本），两者皆无为 0（不触发）。
  - `approve` 且金额 ≥ 阈值时，权限集合须含 `aigc:approve_high` 或 `*:*`，
    否则 `ValueError`（校验先于状态变更，建议保持 pending）。
  - `reject` 不受金额门槛限制（拒绝大单永远比批准安全）。
  - `actor_permissions=None` 表示系统内部调用（闭环自动审核），放行——
    闭环下单量由 ADS 建议与库存公式约束，不经过人工审核通道。
- 路由层（`routes/aigc.py`）：`/review` 与 `/batch-review` 均从认证身份
  （`get_current_user` → `get_user_permissions`）取权限集传入，请求体不可伪造；
  无认证身份（dev 放行）传 None。
- `services/rbac.py` 新增公共函数 `get_user_permissions(user)`。

**权限设计**：`operator` 角色默认**不含** `aigc:approve_high`——高金额建议
需 admin（`*:*`）或专门授予该权限的角色审核。这是审批分级的意义所在。

## F4 会计期间锁定

**痛点**：四个过账入口（采购确认 / 销售开单 / 客户退货入库 / 采购退回出库）
可对任意历史月份过账，已出报表月份可被无痕改写（会计月结的基本红线）。

**实现**：
- `models/erp.py` 新增 `AccountingPeriod(name YYYY-MM unique, is_closed,
  closed_at, closed_by)`。
- `ERPService._assert_period_open(move_date=None)`：无期间记录视为开放
  （未建期间不禁账）；命中已关账期间抛 `ValueError`。四个过账入口在方法
  开头统一调用。
- 期间管理：`list_accounting_periods` / `create_accounting_period`（YYYY-MM
  正则校验 + 唯一约束）/ `close_accounting_period`（幂等，重复关账不覆盖
  首次关账人与时间），均写审计。
- 路由：`GET|POST /api/v1/erp/accounting-periods`、
  `POST /api/v1/erp/accounting-periods/<name>/close`（列表 `erp:read`，
  写操作 `erp:write`）。

## 迁移

`migrations/versions/b00000000002_business_enhancements.py`（幂等：列/表存在
则跳过），SQLite 空库 `flask db upgrade` 实测通过（47 表 + 4 新列），
head = `b00000000002`。

## 测试

`tests/test_business_enhancements.py` 18 用例：
- F1 纯函数（z 表数学验证、含零日语义、非法水位、lead=0）+ 服务层
  （报价供应商交期生效、预览不写回、apply 写回、产品不存在）。
- F2 ETL 注入（帕累托 rank0 强制 A、稳定 SKU→X、波动 SKU→Y、份额/CC 落库）。
- F3 金额计算（报价优先、回退 avg_cost）、权限拒绝（含"校验先于状态变更"）、
  admin 通配通过、系统内部通过、低金额通过、reject 不受门槛。
- F4 期间格式/重复/关账阻断四入口过账、幂等关账、列表倒序。

## 配置项汇总

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `SUGGESTION_HIGH_VALUE_AMOUNT` | 50000 | 高金额审批阈值（数量×单价） |
| `FDE_CLASSIFY_WINDOW_DAYS` | 90 | ABC/XYZ 分级统计窗口天数 |

服务水位与窗口是 `recalc_safety_stock` / 路由参数（非全局配置），按产品调用时指定。

---

# 第二批（P3-G1/G2/G3）：试算平衡 / 对账多对一与调节项 / 库位

## G1 科目余额表（试算平衡）

`ERPService.trial_balance()` + `GET /api/v1/erp/trial-balance`（erp:read）。

AccountMove 是单行凭证模型（无借贷列）：payable/receivable 正记，refund 红字
正记靠 ref_type 区分，cogs 借贷成对（净额≈0）。报表按 (account, ref_type)
聚合净额/税额/条数并给全局总计；同时做 ref_order 断链检查（凭证指向不存在
的采购/销售/退货单时列入 dangling_refs）。

**取舍**：真正的复式双分录改造（每凭证显式借贷两条、科目体系重构）是架构级
变更，单行模型下强行模拟只产出伪平衡；本项落地为科目余额表 + 断链审计，
复式改造留待有明确会计准则合规需求时评估。

## G2 对账多对一匹配 + 人工调节项

- `auto_match` 增强：1:1 未中时，同一 reference_no 候选 2..8 条时穷举 2/3
  条组合，组合金额和落在容差内即整组配平（match_type=multi，星型关联：
  erp→组合首条，组合成员→erp）。典型场景：一张发票分多笔银行流水回款。
- `close_batch` 对齐：multi 匹配按组金额和校验（逐对校验会把 130 vs
  (100+30) 误判差异 30）。
- `ReconciliationService.create_adjustment()` +
  `POST /api/v1/finance/batches/<id>/adjustments`（recon:create）：
  人工调节项（source=manual 明细，必填 reference_no/amount/direction/
  reason），语义为"补记的外部侧事实"——录入后重跑 auto_match 与差异条目
  配平，批次才可关闭。调节项参与外部侧合计（source != erp 口径）。

## G3 库位

- `StockLocation` 模型（erp_stock_location，warehouse 内 code 唯一）；
  `StockMove` 加 from_location_id/to_location_id（nullable，历史流水为 NULL）。
- 约定：调拨两条流水各挂一侧库位（出库挂 from、入库挂 to）；库位现存量按
  StockMove 有符号聚合（事件溯源口径），无独立余额表。
- `transfer_stock` 可选 from_location_id/to_location_id（强制校验库位归属
  仓库且启用）；仓库级余额校验仍是权威约束。
- 端点：GET/POST `/api/v1/erp/locations`（erp:read/erp:write）、
  GET `/api/v1/erp/locations/<id>/stock`（erp:read，产品粒度现存量）。

**不做**：库位级可用量约束（现有校验在仓库级，库位视图是追溯维度）；
收货/发货流水自动挂库位（需先有库位主数据运营流程，当前手工指定）。

## 迁移

`migrations/versions/b00000000003_stock_location.py`（幂等），SQLite 空库
`flask db upgrade` 实测：b1→b2→b3 全链通过（48 表）。

## 测试

`tests/test_phase3_batch2.py` 7 用例：库位 CRUD/唯一性、调拨挂库位与库位
存量视图（±30 对称）、跨仓库库位拒绝、多对一拆分回款配平（星型断言）、
组合超容差不误配、调节项闭环（调节→重跑匹配→关账）、试算平衡聚合与断链。

## 配置项汇总（第二批）

无新增环境配置——库位/调节项均为业务参数（API 请求体），不引入全局开关。
