# Security Policy

## Sensitive data

X Cleaner 不应保存或提交以下敏感信息：

- auth_token
- ct0
- Cookie
- Session
- 登录凭证
- 私有代理账号或密码

请不要在 Issue、Pull Request、日志、截图或 Git 提交中公开这些数据。

## Reporting a vulnerability

如果发现安全问题，请不要在公开 Issue 中提交真实凭证或可利用细节。

建议仅描述：
- 受影响版本
- 问题类型
- 可复现的非敏感步骤
- 预期行为与实际行为

## Credential exposure

如果误将 X 登录 Cookie 或 Token 提交到公开仓库：

1. 立即退出对应 X 登录会话，使旧凭证失效。
2. 删除公开内容。
3. 检查 Git 历史中是否仍包含该凭证。
