"""
Data Agent — Text2SQL 全链路（改进8）
=====================================
在现有 AIGCService.text2sql() 基础上升级为完整全链路：

1. 保留白名单安全底座（沿用 AIGCService.WHITELIST_TABLES）
2. LLM NL→SQL 生成（注入 AIGCService 复用 LLM 后端）
3. SQL AST 安全校验层（sqlparse 解析，验证所有 table 在白名单内 + 禁止 DDL/DML + 强制 LIMIT）
4. 执行查询（通过 SQLAlchemy session.execute 真执行，仅 SELECT，只读事务）
5. LLM 将结果转为自然语言回复（含历史上下文，支持多轮）

设计要点：
- 与 AIGCService 解耦但可注入复用其 LLM 抽象层与 db_session
- SQL 真执行：通过 db.session.execute(text(sql)) 执行，仅 SELECT，结果转 dict 列表
- 安全兜底：sqlparse 不可用 / SQL 校验失败 / 执行异常 → 回退到 AIGCService._rule_query_ads()
- 结果大小限制：最多 100 行，避免 LLM 上下文爆炸
"""

import logging
import re
from datetime import date, datetime

logger = logging.getLogger(__name__)


class DataAgent:
    """Text2SQL 全链路：NL → SQL（LLM 生成）→ 校验（AST）→ 执行 → NL 回复（LLM 润色）"""

    # 最大返回行数（避免 LLM 上下文爆炸 + 防止全表扫描）
    MAX_ROWS = 100

    # 禁止的 SQL 关键字（DDL / DML / 危险操作）
    FORBIDDEN_KEYWORDS = (
        "insert",
        "update",
        "delete",
        "drop",
        "alter",
        "create",
        "truncate",
        "replace",
        "grant",
        "revoke",
        "merge",
        "lock",
        "unlock",
        "vacuum",
        "reindex",
    )

    def __init__(self, aigc_service=None, db_session=None):
        """注入 AIGCService 以复用 LLM 调用、白名单表、规则兜底。"""
        from services.aigc_service import AIGCService

        self._aigc = aigc_service or AIGCService(db_session=db_session)
        self.session = self._aigc.session
        # 复用 AIGCService 的白名单表定义
        self.whitelist_tables = set(self._aigc.WHITELIST_TABLES.keys())

    # ---------------- 主入口 ----------------

    def query(self, question, session_id=None):
        """自然语言查询全链路。

        :param question: 用户问题
        :param session_id: 可选，会话 ID（用于取历史上下文做多轮）
        :return: {session_id, route, sql, result, rows_count, answer}
        """
        sid = session_id or __import__("uuid").uuid4().hex

        # 1. LLM 生成 SQL
        sql = self._generate_sql(question)

        # 2. AST 安全校验
        validated_sql = self._validate_sql_ast(sql) if sql else None

        # 3. 执行查询（真执行）或回退规则
        if validated_sql:
            result, rows_count = self._execute_sql(validated_sql)
            if result is None:
                # 执行失败：回退规则
                logger.warning("[DataAgent] SQL 执行失败，回退规则查询")
                rule_res = self._aigc._rule_query_ads(question)
                result = rule_res["result"]
                rows_count = len(result)
                validated_sql = rule_res["sql"]
                explanation = rule_res["explanation"]
            else:
                explanation = f"LLM 生成 SQL 并真执行（AST 校验通过，{rows_count} 行）"
        else:
            # LLM 未生成 / 校验失败：回退规则
            logger.info("[DataAgent] SQL 生成或校验失败，回退规则查询")
            rule_res = self._aigc._rule_query_ads(question)
            result = rule_res["result"]
            rows_count = len(result)
            validated_sql = rule_res["sql"]
            explanation = rule_res["explanation"]

        # 4. LLM 将结果转自然语言回复
        answer = self._result_to_nl(question, validated_sql, result, explanation, sid)

        # 5. 记录对话历史（user 问 + assistant 答），失败不阻断主流程
        recorded = False
        try:
            from models.aigc import ChatHistory

            self.session.add(ChatHistory(session_id=sid, role="user", content=question))
            self.session.add(ChatHistory(session_id=sid, role="assistant", content=answer))
            self.session.commit()
            recorded = True
        except Exception as e:
            self.session.rollback()
            logger.error("[DataAgent] 记录对话历史失败: %s", e)

        return {
            "session_id": sid,
            "route": "text2sql",
            "sql": validated_sql,
            "result": result,
            "rows_count": rows_count,
            "answer": answer,
            "_recorded": recorded,
        }

    # ---------------- 1. LLM NL→SQL 生成 ----------------

    def _generate_sql(self, question):
        """LLM 生成 SQL（注入白名单 schema 作为 context）。"""
        if not self._aigc._llm_available():
            logger.info("[DataAgent] LLM 不可用，跳过 SQL 生成")
            return None

        schema = "\n".join(self._aigc.WHITELIST_TABLES.values())
        # 获取今天日期作为上下文（LLM 需要知道"今天"/"昨天"对应的日期）
        today = date.today().isoformat()
        yesterday = date.fromordinal(date.today().toordinal() - 1).isoformat()

        prompt = [
            {
                "role": "system",
                "content": (
                    "你是 Text2SQL 引擎。仅可查询以下白名单表：\n"
                    f"{schema}\n\n"
                    "规则：\n"
                    "1. 只生成 SELECT 语句；\n"
                    "2. 必须包含 LIMIT（最大 100）；\n"
                    "3. 禁止 INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/TRUNCATE 及任何写操作子查询；\n"
                    "4. 只能查询上述白名单表，禁止查其他表；\n"
                    "5. 日期字段用 YYYY-MM-DD 格式；\n"
                    f"6. 今天是 {today}，昨天是 {yesterday}；\n"
                    "7. 只返回一条 SQL，不要解释、不要 markdown 代码块。"
                ),
            },
            {"role": "user", "content": question},
        ]
        resp = self._aigc._call_llm(prompt, temperature=0.1)
        if not resp:
            return None
        # 清理 LLM 输出：去除 markdown 代码块标记
        sql = resp.strip()
        sql = re.sub(r"^```(?:sql)?\s*", "", sql)
        sql = re.sub(r"\s*```$", "", sql)
        sql = sql.strip().strip("`").rstrip(";").strip()
        logger.info("[DataAgent] LLM 生成 SQL: %s", sql)
        return sql

    # ---------------- 2. SQL AST 安全校验 ----------------

    def _validate_sql_ast(self, sql):
        """SQL AST 安全校验：使用 sqlparse 解析 + 多层校验。

        校验项：
        - 必须是单个 SELECT/WITH 语句
        - 禁止 DDL/DML 关键字
        - 所有 table 必须在白名单内
        - 必须有 LIMIT（没有则自动补 LIMIT 100）
        - 禁止子查询中的写操作
        :return: 校验通过返回规范化 SQL，否则 None
        """
        if not sql:
            return None

        try:
            import sqlparse
        except ImportError:
            logger.warning("[DataAgent] sqlparse 未安装，回退关键字校验")
            return self._fallback_keyword_validate(sql)

        try:
            statements = sqlparse.parse(sql)
        except Exception as e:
            logger.warning("[DataAgent] SQL 解析失败: %s", e)
            return None

        if not statements or len(statements) != 1:
            logger.warning("[DataAgent] SQL 必须为单条语句，实际 %d 条", len(statements) if statements else 0)
            return None

        stmt = statements[0]
        sql_text = str(stmt).strip().rstrip(";").strip()
        low = sql_text.lower()

        # 1. 必须以 SELECT 或 WITH 开头
        if not (low.startswith("select") or low.startswith("with")):
            logger.warning("[DataAgent] SQL 校验失败：非 SELECT/WITH 开头")
            return None

        # 2. 遍历所有 token，检查禁止关键字 + 提取表名
        tables_in_sql = set()
        for token in stmt.flatten():
            token_str = str(token).lower()
            # 禁止关键字检查
            for kw in self.FORBIDDEN_KEYWORDS:
                # 精确匹配关键字（避免误判字段名包含 update 等）
                if re.search(rf"\b{kw}\b", token_str):
                    logger.warning("[DataAgent] SQL 校验失败：包含禁止关键字 %s", kw)
                    return None

        # 提取表名：查找 FROM / JOIN 后面的标识符
        tables_in_sql = self._extract_tables(stmt)

        # 3. 白名单校验
        for t in tables_in_sql:
            if t not in self.whitelist_tables:
                logger.warning("[DataAgent] SQL 校验失败：表 %s 不在白名单内", t)
                return None

        # 4. 强制 LIMIT
        if "limit" not in low:
            sql_text = sql_text + " LIMIT 100"
            logger.info("[DataAgent] SQL 无 LIMIT，自动补 LIMIT 100")

        logger.info("[DataAgent] SQL AST 校验通过，表=%s", tables_in_sql)
        return sql_text

    def _extract_tables(self, stmt, cte_aliases=None):
        """从 sqlparse Statement 中提取 FROM / JOIN 后的表名。

        :param cte_aliases: WITH 子句定义的 CTE 别名集合（这些别名不是真实表，跳过白名单校验）
        """
        if cte_aliases is None:
            cte_aliases = set()
        tables = set()
        from_seen = False
        join_seen = False
        with_seen = False
        for token in stmt.tokens:
            # 跳过 whitespace 和 punctuation（不重置 from_seen/join_seen）
            if token.is_whitespace or str(token).strip() in (",", ""):
                continue
            if token.is_keyword:
                kw = str(token).lower()
                if kw == "from":
                    from_seen = True
                    join_seen = False
                    with_seen = False
                    continue
                elif kw.startswith("join"):
                    join_seen = True
                    from_seen = False
                    with_seen = False
                    continue
                elif kw == "with":
                    with_seen = True
                    continue
                elif kw in ("where", "group", "order", "limit", "having", "union"):
                    from_seen = False
                    join_seen = False
                    with_seen = False
                    continue
            # WITH 子句：提取 CTE 别名 + 递归处理 CTE 内的真实表名
            if with_seen and hasattr(token, "tokens"):
                # CTE 定义形如 "alias AS (SELECT ...)"，alias 是 CTE 名
                if hasattr(token, "get_real_name"):
                    cte_name = token.get_real_name()
                    if cte_name:
                        cte_aliases.add(cte_name.lower())
                # 递归提取 CTE 子查询内的真实表名
                tables.update(self._extract_tables(token, cte_aliases))
                continue
            if from_seen or join_seen:
                # 处理 Identifier 或 IdentifierList
                if hasattr(token, "get_real_name"):
                    name = token.get_real_name()
                    if name:
                        name_lower = name.lower()
                        # CTE 别名跳过白名单校验（不是真实表）
                        if name_lower not in cte_aliases:
                            tables.add(name_lower)
                elif hasattr(token, "get_identifiers"):
                    for ident in token.get_identifiers():
                        if hasattr(ident, "get_real_name"):
                            name = ident.get_real_name()
                            if name:
                                name_lower = name.lower()
                                if name_lower not in cte_aliases:
                                    tables.add(name_lower)
                elif token.ttype is None and hasattr(token, "tokens"):
                    # 子查询 — 递归处理
                    tables.update(self._extract_tables(token, cte_aliases))
                from_seen = False
                join_seen = False
        return tables

    def _fallback_keyword_validate(self, sql):
        """sqlparse 不可用时的关键字校验兜底（与 AIGCService._validate_sql 一致）。"""
        if not sql:
            return None
        s = sql.strip().strip("`").rstrip(";").strip()
        low = s.lower()
        if not (low.startswith("select") or low.startswith("with")):
            return None
        for kw in self.FORBIDDEN_KEYWORDS:
            if re.search(rf"\b{kw}\b", low):
                return None
        if re.search(r"\(\s*(insert|update|delete|drop|alter|create)\s", low):
            return None
        if "limit" not in low:
            s = s + " LIMIT 100"
        return s

    # ---------------- 3. 执行查询 ----------------

    def _execute_sql(self, sql):
        """执行 SELECT SQL，返回 (result_list, rows_count)。

        - 使用 SQLAlchemy session.execute(text(sql)) 真执行
        - 仅 SELECT，只读事务（不 commit）
        - 结果转 dict 列表，最多 MAX_ROWS 行
        - 异常返回 (None, 0)
        """
        try:
            from sqlalchemy import text

            result = self.session.execute(text(sql))
            rows = result.fetchmany(self.MAX_ROWS)
            if not rows:
                return [], 0
            # 取列名
            columns = list(result.keys()) if hasattr(result, "keys") else []
            result_list = []
            for row in rows:
                row_dict = {}
                for i, col in enumerate(columns):
                    val = row[i] if i < len(row) else None
                    # 序列化日期/时间
                    if isinstance(val, (date, datetime)):
                        val = val.isoformat()
                    elif hasattr(val, "__float__"):
                        val = float(val)
                    row_dict[col] = val
                result_list.append(row_dict)
            logger.info("[DataAgent] SQL 执行成功，返回 %d 行", len(result_list))
            return result_list, len(result_list)
        except Exception as e:
            logger.error("[DataAgent] SQL 执行失败: %s", e)
            return None, 0

    # ---------------- 4. 结果转自然语言 ----------------

    def _result_to_nl(self, question, sql, result, explanation, session_id):
        """LLM 将查询结果转为自然语言回复。

        - 无 LLM：用 _format_sql_result 模板
        - 有 LLM：把结果喂给 LLM 做自然语言总结
        """
        # 模板兜底（永远先准备一份）
        template = self._format_result(question, sql, result, explanation)

        if not self._aigc._llm_available() or not result:
            return template

        # 取历史上下文（最近 5 条）
        history_ctx = self._get_history_context(session_id)

        # 结果截断（避免上下文爆炸）
        result_str = str(result[:20])  # 最多喂 20 行给 LLM
        if len(result_str) > 2000:
            result_str = result_str[:2000] + "...(截断)"

        prompt = [
            {
                "role": "system",
                "content": (
                    "你是数据分析师助手。根据 SQL 查询结果用简洁中文回答用户问题。"
                    "要点：1) 直接回答问题；2) 给出关键数字；3) 必要时列出 top 3-5 项；"
                    "4) 不超过 150 字；5) 不要暴露 SQL 细节。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"对话历史：\n{history_ctx}\n"
                    f"用户问题：{question}\n"
                    f"执行的 SQL：{sql}\n"
                    f"查询结果（{len(result)} 行）：\n{result_str}"
                ),
            },
        ]
        try:
            refined = self._aigc._call_llm(prompt, temperature=0.3)
            if refined and refined.strip():
                return refined.strip()
        except Exception as e:
            logger.warning("[DataAgent] LLM 结果转 NL 失败: %s，回退模板", e)
        return template

    def _format_result(self, question, sql, result, explanation):
        """模板格式化结果（LLM 不可用时的兜底）。"""
        if not result:
            return f"{explanation}\n查询结果为空。"
        lines = [f"{explanation}", f"共 {len(result)} 条结果："]
        for i, row in enumerate(result, 1):
            if i > 10:
                lines.append(f"...（共 {len(result)} 条，仅显示前 10 条）")
                break
            lines.append(f"{i}. {row}")
        return "\n".join(lines)

    def _get_history_context(self, session_id, limit=5):
        """取最近对话历史作为上下文（用于多轮）。"""
        if not session_id:
            return ""
        try:
            from models.aigc import ChatHistory

            recent = (
                self.session.query(ChatHistory)
                .filter(ChatHistory.session_id == session_id)
                .order_by(ChatHistory.created_at.desc())
                .limit(limit)
                .all()
            )
            recent.reverse()
            if not recent:
                return ""
            # 截断单条消息 + 总长度
            MAX_MSG = 500
            MAX_TOTAL = 2000
            truncated = []
            total = 0
            for m in reversed(recent):
                content = (m.content or "")[:MAX_MSG]
                if total + len(content) > MAX_TOTAL:
                    remain = MAX_TOTAL - total
                    if remain > 50:
                        truncated.insert(0, f"{m.role}: {content[:remain]}...(截断)")
                    break
                truncated.insert(0, f"{m.role}: {content}")
                total += len(content)
            return "\n".join(truncated)
        except Exception as e:
            logger.debug("[DataAgent] 历史上下文获取失败: %s", e)
            return ""
