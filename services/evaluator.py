"""
Agent 评测框架 — Text2SQL gold SQL 回归测试
==========================================
在 DataAgent Text2SQL 全链路基础上，引入 gold SQL 测试用例集，
对 prompt 改动后的 SQL 生成准确率做自动化回归检测。

核心流程：
1. 从 tests/fixtures/gold_sql.json 加载 gold SQL 测试用例
2. 调用 DataAgent._generate_sql 生成实际 SQL（测试中可 monkeypatch）
3. 多级对比：完全匹配 > 表名集合匹配 > 结果集匹配
4. 与上次评测报告对比，检测退化（accuracy_delta / new_failures / new_passes）
5. 报告保存为 JSON 到 tests/fixtures/eval_reports/ 目录

设计要点：
- 不依赖真实 LLM 调用：_generate_sql 可被 monkeypatch 替换
- gold 用例白名单表与 AIGCService.WHITELIST_TABLES 一致
- 评测报告保存为 JSON，latest.json 用于下次对比
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# gold 用例与评测报告的文件路径（基于项目根目录）
_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
_GOLD_CASES_FILE = _FIXTURES_DIR / "gold_sql.json"
_REPORTS_DIR = _FIXTURES_DIR / "eval_reports"


class AgentEvaluator:
    """Agent 评测框架：Text2SQL gold SQL 回归测试。"""

    def __init__(self, data_agent=None, db_session=None, reports_dir=None):
        """初始化评测器。

        :param data_agent: 注入 DataAgent 实例；不传则默认创建（测试中可 monkeypatch _generate_sql）
        :param db_session: 可选，注入数据库 session
        :param reports_dir: 可选，自定义评测报告保存目录（测试用 tmp_path 隔离）
        """
        if data_agent is None:
            from services.data_agent import DataAgent

            data_agent = DataAgent(db_session=db_session)
        self._agent = data_agent
        self.session = self._agent.session
        # 报告目录可覆盖，便于测试隔离
        self.reports_dir = Path(reports_dir) if reports_dir else _REPORTS_DIR

    # ==================== 主入口 ====================

    def run_evaluation(self, test_cases=None, category=None):
        """运行一批 gold SQL 测试用例，返回评测报告。

        :param test_cases: 可选，指定测试用例子集；默认跑全部
        :param category: 可选，按类别筛选（aggregation/join/with/limit/filter）
        :return: {
            total, passed, failed, accuracy,
            details: [{id, question, category, expected_sql, actual_sql, match_type, passed}],
            comparison: {vs_last_run: {accuracy_delta, new_failures, new_passes} | None},
            run_at
        }
        """
        cases = test_cases if test_cases is not None else self.load_gold_cases()
        if category:
            cases = [c for c in cases if c.get("category") == category]

        details = []
        passed = 0
        for case in cases:
            question = case["question"]
            expected_sql = case["expected_sql"]
            expected_tables = case.get("expected_tables", [])

            # 调 DataAgent 生成 SQL（测试中可 monkeypatch _generate_sql）
            try:
                actual_sql = self._agent._generate_sql(question)
            except Exception as e:
                logger.warning("用例 %s 生成 SQL 失败: %s", case.get("id"), e)
                actual_sql = None

            match_type, matched = self._compare_sql(actual_sql, expected_sql, expected_tables)

            if matched:
                passed += 1

            details.append(
                {
                    "id": case.get("id"),
                    "question": question,
                    "category": case.get("category"),
                    "expected_sql": expected_sql,
                    "actual_sql": actual_sql,
                    "match_type": match_type,
                    "passed": matched,
                }
            )

        total = len(cases)
        accuracy = round(passed / total, 4) if total else 0.0

        # 加载上次报告，构建退化对比
        last_report = self._load_last_report()
        comparison = self._build_comparison(accuracy, details, last_report)

        report = {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "accuracy": accuracy,
            "details": details,
            "comparison": comparison,
            "run_at": datetime.now().isoformat(),
        }

        self._save_report(report)
        return report

    def load_gold_cases(self):
        """从 tests/fixtures/gold_sql.json 加载 gold SQL 测试用例。

        :return: list[dict] 每条 {id, question, expected_sql, expected_tables, category}
        """
        with open(_GOLD_CASES_FILE, encoding="utf-8") as f:
            return json.load(f)

    # ==================== SQL 对比 ====================

    def _compare_sql(self, actual, expected, expected_tables=None):
        """对比 SQL：完全匹配 > 表名集合匹配 > 结果集匹配。

        :param actual: LLM 生成的 SQL（可能为 None）
        :param expected: gold SQL
        :param expected_tables: 可选，gold 用例声明的期望表名集合
        :return: (match_type, passed) — match_type: exact/table_set/result_set/none
        """
        if not actual:
            return "none", False

        # 规范化后比对
        norm_actual = self._normalize_sql(actual)
        norm_expected = self._normalize_sql(expected)

        # 1. 完全匹配
        if norm_actual == norm_expected:
            return "exact", True

        # 2. 表名集合匹配
        actual_tables = self._extract_tables(actual)
        if expected_tables:
            expected_set = {t.lower() for t in expected_tables}
        else:
            expected_set = self._extract_tables(expected)

        if actual_tables and actual_tables == expected_set:
            return "table_set", True

        # 3. 结果集匹配（尝试执行两条 SQL 对比结果）
        if self._result_set_match(actual, expected):
            return "result_set", True

        return "none", False

    def _normalize_sql(self, sql):
        """规范化 SQL 用于精确比对：去分号/反引号、统一小写、压缩空白。"""
        if not sql:
            return ""
        s = sql.strip().strip("`").rstrip(";").strip().lower()
        s = re.sub(r"\s+", " ", s)
        return s

    def _extract_tables(self, sql):
        """从 SQL 中提取 FROM/JOIN 后的表名（正则匹配，用于表名集合对比）。"""
        if not sql:
            return set()
        tables = set()
        # 匹配 FROM table / JOIN table（含别名）
        pattern = r"(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)"
        for m in re.finditer(pattern, sql, re.IGNORECASE):
            tables.add(m.group(1).lower())
        return tables

    def _result_set_match(self, actual, expected):
        """尝试执行两条 SQL 对比结果集（仅 SELECT，失败或空结果返回 False）。

        - 两条 SQL 在同一 session 上执行，只读不 commit
        - 空结果集不判定为匹配（DB 可能无数据，无法区分）
        - 行数不同或任意行不一致均返回 False
        """
        try:
            from sqlalchemy import text

            r1 = self.session.execute(text(actual))
            rows1 = r1.fetchall()
            r2 = self.session.execute(text(expected))
            rows2 = r2.fetchall()

            # 空结果集不判定匹配（DB 无数据时无法区分）
            if len(rows1) == 0 and len(rows2) == 0:
                return False

            if len(rows1) != len(rows2):
                return False

            return all(tuple(a) == tuple(b) for a, b in zip(rows1, rows2, strict=False))
        except Exception as e:
            logger.debug("结果集匹配失败: %s", e)
            return False

    # ==================== 退化检测 ====================

    def _build_comparison(self, accuracy, details, last_report):
        """构造与上次评测的对比信息（退化检测）。"""
        if not last_report:
            return {"vs_last_run": None}

        last_accuracy = last_report.get("accuracy", 0.0)
        last_passed_ids = {d["id"] for d in last_report.get("details", []) if d.get("passed")}
        cur_passed_ids = {d["id"] for d in details if d.get("passed")}

        new_failures = sorted(last_passed_ids - cur_passed_ids)
        new_passes = sorted(cur_passed_ids - last_passed_ids)

        return {
            "vs_last_run": {
                "accuracy_delta": round(accuracy - last_accuracy, 4),
                "new_failures": new_failures,
                "new_passes": new_passes,
            }
        }

    # ==================== 报告持久化 ====================

    def _save_report(self, report):
        """保存评测报告到 tests/fixtures/eval_reports/ 目录，用于下次对比。

        - 按时间戳保存历史报告 eval_YYYYMMDD_HHMMSS.json
        - 同时覆盖 latest.json 便于下次对比加载
        """
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.reports_dir / f"eval_{ts}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        # 覆盖 latest.json，供下次 _load_last_report 使用
        latest = self.reports_dir / "latest.json"
        with open(latest, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        logger.info("评测报告已保存: %s", path)
        return path

    def _load_last_report(self):
        """加载上次评测报告（latest.json），用于退化检测。

        :return: dict 或 None（无历史报告时）
        """
        latest = self.reports_dir / "latest.json"
        if not latest.exists():
            return None
        try:
            with open(latest, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("加载上次评测报告失败: %s", e)
            return None


# ==================== CLI 入口 ====================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Agent 评测框架：Text2SQL gold SQL 回归测试")
    parser.add_argument(
        "--category",
        default=None,
        help="只跑指定类别（aggregation/join/with/limit/filter），不传则跑全部",
    )
    args = parser.parse_args()

    # CLI 运行需要 Flask app context（DataAgent 依赖 db.session）
    from app import create_app

    flask_app = create_app()
    with flask_app.app_context():
        evaluator = AgentEvaluator()
        report = evaluator.run_evaluation(category=args.category)

        # 控制台输出评测摘要
        print(f"\n{'=' * 60}")
        print(f"Text2SQL 评测完成：{report['passed']}/{report['total']} 通过，准确率 {report['accuracy'] * 100:.1f}%")
        print(f"{'=' * 60}")
        for d in report["details"]:
            status = "PASS" if d["passed"] else "FAIL"
            print(f"  [{status}] [{d['category']}] {d['question']}  (match: {d['match_type']})")

        comp = report.get("comparison", {}).get("vs_last_run")
        if comp:
            print(f"\n与上次对比：准确率变化 {comp['accuracy_delta']:+.1%}")
            if comp["new_failures"]:
                print(f"  新增失败用例：{comp['new_failures']}")
            if comp["new_passes"]:
                print(f"  新增通过用例：{comp['new_passes']}")
        else:
            print("\n（首次评测，无历史对比）")
