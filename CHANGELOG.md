# 变更日志

本项目遵循 [Conventional Commits](https://www.conventionalcommits.org/) 规范的提交信息，本文件由 [conventional-changelog](https://github.com/conventional-changelog/conventional-changelog) 自动生成并维护。

所有显著变更均会记录于此文件中。版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

---

## [1.0.0] - 2026-07-30

### Features

- feat(agent): 引入 LangGraph StateGraph 实现 Agent 闭环编排，替代硬编码状态机，支持条件分支与并行节点
- feat(agent): 新增 Agent 评测框架，覆盖准确率、延迟、Token 消耗等多维度指标
- feat(sql): 采用 sqlglot 替代 sqlparse 进行 SQL AST 校验，精确追踪 WITH 别名与表引用
- feat(auth): 新增 JWT token_version 字段，支持会话级令牌吊销
- feat(auth): 实现 RBAC 角色权限模型，按角色限定接口与资源访问
- feat(auth): 新增 MFA TOTP 双因素认证，绑定密钥与一次性校验码
- feat(auth): 采用 HttpOnly + Secure + SameSite=Strict cookie 存储 token，Authorization Header 向后兼容
- feat(security): 引入字段级加密（AES-GCM）保护敏感数据落库
- feat(monitor): 新增慢查询监控与告警链路，超阈值自动记录并上报
- feat(monitor): 接入 Loki 日志聚合，统一收集应用与访问日志
- feat(monitor): 接入 Alertmanager 告警通道，支持分级路由与静默
- feat(metrics): 新增业务 KPI 指标采集与展示，覆盖核心运营场景
- feat(db): 实现读写分离，主库写入、只读副本承接查询流量
- feat(db): 按时间维度对大表进行分区，降低单分区扫描成本
- feat(cache): 构建多级缓存（本地缓存 + Redis），降低热点数据回源率
- feat(fin): 新增财务对账模块，支持多通道流水核对与差异输出
- feat(fin): 实现 BOM（物料清单）与 MRP（物料需求计划）基础能力
- feat(frontend): 增强可访问性，补全 ARIA 属性、焦点管理与语义化标签
- feat(frontend): 响应式布局适配，覆盖桌面、平板、移动端断点
- feat(devops): 提供 K8s Helm chart，支持一键部署
- feat(devops): 配置 HPA 水平 Pod 自动伸缩，按负载动态扩缩容

### Bug Fixes

- fix(auth): 修复会话过期后刷新页面仍保留 UI 权限的显示问题
- fix(sql): 修复 WITH 子句别名在嵌套查询中被错误解析的问题
- fix(monitor): 修复慢查询采样在高并发下丢失样本的统计偏差
- fix(cache): 修复缓存击穿场景下回源风暴未有效合并的问题
- fix(fin): 修复对账跨日时区偏移导致金额不一致的问题

### Security

- security(auth): sessionStorage 存储 token 受 XSS 威胁，迁移至 HttpOnly cookie
- security(auth): JWT 引入 token_version，登出与改密后旧令牌立即失效
- security(data): 敏感字段（手机号、身份证号）落库前进行字段级加密
- security(web): cookie 增加 Secure 与 SameSite=Strict 属性，缓解 CSRF
- security(deps): 锁定 sqlglot < 26.0，规避上游破坏性变更

### Documentation

- docs(adr): 新增 ADR 0001 记录采用 LangGraph 进行 Agent 编排的决策
- docs(adr): 新增 ADR 0002 记录采用 sqlglot 进行 SQL 校验的决策
- docs(adr): 新增 ADR 0003 记录采用 HttpOnly cookie 存储认证 token 的决策
- docs(build): 新增前端构建链说明与 CHANGELOG 生成流程
- docs(api): 补充 Agent 评测框架与监控指标接入文档

### Performance

- perf(db): 读写分离后查询吞吐提升，主库写入压力下降
- perf(db): 表分区后大表扫描范围收敛，P95 查询延迟下降
- perf(cache): 多级缓存命中率达预期，热点接口回源率显著降低
- perf(frontend): 接入 clean-css / terser 产物压缩，首屏资源体积下降
- perf(agent): LangGraph 并行节点缩短单轮编排耗时
