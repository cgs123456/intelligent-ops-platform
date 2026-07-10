# Data Agent SQL 解析器替换：sqlparse → sqlglot

**日期**：2026-07-10
**状态**：已确认，待编写实现计划
**子项目**：Agent 编排三方向之一（独立于 LangGraph 升级、Agent 评测框架）

## 1. 背景与动机

`services/data_agent.py` 的 Text2SQL 全链路依赖 sqlparse 做 SQL AST 安全校验，主要痛点：

1. **CTE 别名追踪脆弱**：`_extract_tables` 用 `from_seen` / `join_seen` / `with_seen` 状态机手动遍历 token，对嵌套 WITH 作用域处理不可靠；`get_real_name()` 对 `schema.table` 格式行为不一致。
2. **列名误判**：`FORBIDDEN_KEYWORDS` 用 `\b{kw}\b` 正则匹配每个 token，列名 `u.update` 会被误判为 DML `UPDATE`，导致合法 SQL 被拦。

sqlglot 提供真正的 AST：
- `exp.Table` 节点的 `.name` 统一返回表名部分
- `exp.CTE` 节点的 `.alias` 是 WITH 子句定义的别名
- `exp.Update` / `exp.Column` 类型区分天然消除列名误判
- 纯 Python 无 C 扩展依赖，安装稳定

## 2. 范围

### 2.1 改动文件

| 文件 | 改动 |
|------|------|
| `services/data_agent.py` | 重写 `_validate_sql_ast`、`_extract_tables`；删除 `_fallback_keyword_validate` |
| `requirements.txt` | 移除 `sqlparse>=0.4,<1.0`；新增 `sqlglot>=23.0,<25.0` |
| `tests/test_data_agent.py` | 保留行为一致的用例；新增 AST 优势用例；删除 `_fallback_keyword_validate` 相关用例（如有） |

### 2.2 不变部分

- `query()` 主流程、`_generate_sql()`、`_execute_sql()`、`_result_to_nl()`
- `DataAgent.__init__` 签名、白名单表定义来源（`AIGCService.WHITELIST_TABLES`）
- `FORBIDDEN_KEYWORDS` 常量保留（仅供日志展示）
- `requirements-dev.txt` 无需改动

### 2.3 替换策略

**完全替换**：移除 sqlparse 依赖，不保留 fallback。sqlglot 纯 Python 无依赖，解析失败直接返回 None 回退规则查询，不需要关键字兜底。

## 3. 详细设计

### 3.1 `_validate_sql_ast` 重写

**输入**：LLM 生成的 SQL 字符串（可能含 markdown 包裹、尾部分号）
**输出**：校验通过返回规范化 SQL（末尾补 `LIMIT 100`）；失败返回 None

**校验步骤**（按顺序）：

1. **解析**：`sqlglot.parse_one(sql, dialect="sqlite")`
   - 抛 `sqlglot.errors.ParseError` → log warning + 返回 None
   - 显式 `dialect="sqlite"`：项目测试用 SQLite、生产用 PostgreSQL，sqlglot 默认方言兼容两者，显式 sqlite 更安全
2. **单语句校验**：`parse_one` 本身只返回单条语句，多语句会抛 `ParseError`，天然满足"单条 SELECT/WITH"约束（sqlparse 需手动检查 `len(statements) != 1`）
3. **根节点类型校验**：必须是 `exp.Select` / `exp.Union` / `exp.With`（WITH 包装的 SELECT）
4. **禁止 DDL/DML 检查**（AST 类型）：
   ```python
   FORBIDDEN_NODE_TYPES = (
       exp.Insert, exp.Update, exp.Delete, exp.Drop,
       exp.Alter, exp.Create, exp.Merge, exp.TruncateTable,
   )
   for node_type in FORBIDDEN_NODE_TYPES:
       if expression.find(node_type):
           log warning + return None
   ```
   - `REPLACE` / `GRANT` / `REVOKE` / `LOCK` / `VACUUM` / `REINDEX` 在 SQLite 方言下非有效语法，`parse_one` 会抛 `ParseError`，天然拦截
   - `FORBIDDEN_KEYWORDS` 常量保留但仅用于日志展示，实际校验用 AST 类型
5. **表名提取 + 白名单校验**：调用 `_extract_tables(expression)`，对每个表名检查是否在 `self.whitelist_tables`
6. **强制 LIMIT**：`if not expression.find(exp.Limit):` 通过 `expression.set("limit", exp.Limit(expression=exp.Literal.number(100)))` 设置 Limit 节点（AST 操作，非字符串拼接）
7. **返回**：`expression.sql(dialect="sqlite")` 生成规范化 SQL 字符串

**关键改进**：列名 `u.update` 不会再误判（它是 `exp.Column`，不是 `exp.Update`）。

### 3.2 `_extract_tables` 重写

**输入**：sqlglot Expression 对象
**输出**：`set[str]` 真实表名集合（小写）

```python
def _extract_tables(self, expression):
    """从 sqlglot Expression 中提取真实表名（过滤 CTE 别名）。

    sqlglot AST 优势：
    - exp.Table 节点的 .name 是表名（已去除 schema/db 前缀）
    - exp.CTE 节点的 .alias 是 WITH 子句定义的别名
    - 嵌套 WITH 作用域由 AST 结构天然区分，不需要手动维护 cte_aliases 状态
    """
    tables = set()
    # 1. 收集所有 CTE 别名（WITH 子句定义的临时表名）
    cte_aliases = {cte.alias.lower() for cte in expression.find_all(exp.CTE)}
    # 2. 收集所有表引用，过滤掉 CTE 别名
    for table in expression.find_all(exp.Table):
        name = (table.name or "").lower()
        if name and name not in cte_aliases:
            tables.add(name)
    return tables
```

**对比 sqlparse 版本**：
- 不再需要 `from_seen` / `join_seen` / `with_seen` 状态机
- `find_all(exp.Table)` 递归遍历整个 AST，子查询中的表也会被收集
- `table.name` 统一返回表名部分（sqlparse 的 `get_real_name()` 行为不一致）

**边界情况**：
- `table.name` 为空（如 `SELECT * FROM (SELECT ...) AS t` 中的子查询）→ 跳过（子查询不是表）
- 同一表多次引用（JOIN 同一表）→ `set` 自动去重
- 嵌套 CTE（CTE 内部引用外层 CTE 别名）→ 外层 CTE 别名在 `cte_aliases` 集合中，会被正确过滤

### 3.3 删除 `_fallback_keyword_validate`

sqlglot 纯 Python 无依赖，`parse_one` 失败直接返回 None，触发 `query()` 主流程的规则回退（`_rule_query_ads`）。不需要关键字校验兜底。

## 4. 测试设计

### 4.1 保留的用例（行为一致）

| 用例 | 预期 |
|------|------|
| 合法 SELECT 校验通过 | 返回规范化 SQL |
| 非法 DDL（`DROP TABLE`）拦截 | 返回 None |
| 非法 DML（`DELETE FROM`）拦截 | 返回 None |
| 白名单外表名拦截 | 返回 None |
| 无 LIMIT 自动补全 | 末尾追加 `LIMIT 100` |

### 4.2 新增用例（AST 优势）

| 用例 | SQL | 预期 |
|------|-----|------|
| 列名含关键字 | `SELECT u.update FROM users u` | 校验通过，表名提取 `{"users"}` |
| 嵌套 CTE 表名提取 | `WITH t1 AS (SELECT * FROM a), t2 AS (SELECT * FROM t1 JOIN b) SELECT * FROM t2` | 表名 `{"a", "b"}` |
| 多语句 SQL 拦截 | `SELECT 1; DROP TABLE x;` | 返回 None（`parse_one` 抛 ParseError） |

### 4.3 删除的用例

已确认现有 `test_data_agent.py` 中无 `_fallback_keyword_validate` 相关用例（grep 验证），无需删除。

## 5. 错误处理

| 场景 | 行为 |
|------|------|
| `sqlglot.parse_one()` 抛 `ParseError` | log warning + 返回 None（与 sqlparse 时代 `parse` 失败一致） |
| `expression.find()` / `find_all()` | AST 已解析成功，不抛异常，无需额外 try-except |
| `expression.sql()` 生成 SQL | AST 有效不会失败，但用 try-except 包裹，失败返回 None |
| 校验失败 | `query()` 主流程回退 `_rule_query_ads()` 规则查询 |

## 6. CI 集成

- `requirements.txt` 移除 sqlparse 新增 sqlglot → CI 自动安装
- 现有 `test_data_agent.py` 9 个用例 + 新增约 3 个用例 = 12 个用例
- 全部应在 CI 的 Python 3.10/3.11/3.12 矩阵下通过
- `requirements-dev.txt` 无需改动

## 7. 验收标准

1. `sqlparse` 从 `requirements.txt` 移除，`sqlglot` 新增
2. `services/data_agent.py` 中无 `import sqlparse`
3. `test_data_agent.py` 全部用例通过（Python 3.10/3.11/3.12）
4. 新增 3 个用例覆盖 AST 优势（列名含关键字、嵌套 CTE、多语句拦截）
5. CI 流水线（lint/test/security/build）全部通过
6. `pyproject.toml` 的 ruff/mypy/black 配置无需调整（sqlglot 代码遵循现有风格）
