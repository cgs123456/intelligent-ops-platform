# ADR 0003: 采用 HttpOnly cookie 存储认证 token

## 状态

已采纳

## 背景

平台原方案将 JWT 存储在浏览器 `sessionStorage` 中，前端通过 JS 读取后在每次请求时附带 `Authorization` 头。该方案在 XSS 攻击下存在风险：一旦页面被注入恶意脚本，攻击者可直接读取 `sessionStorage` 中的 token 并冒用身份。

需要一种在 XSS 场景下能降低 token 泄露面的存储与传输方案，同时兼顾向后兼容与已有接入方。本决策对应任务编号 **P1-12**。

## 决策

采用 **HttpOnly + Secure + SameSite=Strict** 的 cookie 存储 token，并保留 `Authorization` Header 向后兼容。

具体要点：

- 服务端在登录成功后通过 `Set-Cookie` 下发 token，cookie 设置 `HttpOnly`（JS 不可读）、`Secure`（仅 HTTPS 传输）、`SameSite=Strict`（跨站不携带）。
- 前端请求统一携带 `credentials: 'include'`，使浏览器自动附带 cookie，无需 JS 读取 token。
- `Authorization: Bearer <token>` Header 仍被服务端识别，作为向后兼容通道，供无法使用 cookie 的客户端（如脚本、服务端到服务端调用）使用。
- 服务端鉴权中间件优先校验 cookie 中的 token，其次校验 `Authorization` Header。

## 结果

- **正面**：token 不再暴露给 JS，XSS 即便发生也无法直接读取 token，显著降低冒用风险。
- **约束**：所有跨域请求需携带 `credentials: 'include'`；CORS 配置需设置 `supports_credentials=True`，且 `Access-Control-Allow-Origin` 不能为 `*`，必须明确指定来源。
- **CSRF**：由 `SameSite=Strict` 缓解——跨站请求不会携带 cookie；如未来有 GET 安全写操作或第三方表单提交需求，需补充 CSRF token 方案。
- **兼容**：旧客户端通过 `Authorization` Header 继续可用，迁移期间两套通道并存。
- **后续**：在文档与前端 SDK 中明确 `credentials: 'include'` 的接入要求，并在 CORS 配置中固化白名单来源。
