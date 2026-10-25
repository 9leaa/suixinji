# 控制台多用户本地验收

功能开发分支：`codex/console-multiuser`，集成目标为 `main`。当前只在本地测试环境启用，未部署服务器。

## 使用方式

打开 `http://127.0.0.1:18080/console`，点击“注册新账号”。用户名为 3–32 位字母、数字、下划线或短横线，不区分大小写；密码 12–128 位。注册时设置称呼。

每个账号创建独立 tenant、user 和 personal space。普通用户只能访问自己的消息、回答、记忆和版本来源；消息与化名修改使用服务端会话身份，忽略客户端伪造的 user/space。历史数据不自动归入任何新账号。

管理员可以查看自己空间的完整执行图、模型输入输出和整理观察记录；管理员也不能跨账号查询。全站用户管理和跨账号排障入口尚未实现。普通用户隐藏并限制原始运行详情及 `/status`、`/trace` 等诊断命令；仍可通过已有记忆命令管理自己的记忆。

本地管理员用户名 `local_admin`，随机密码仅保存在忽略 Git 的 `environments/test/data/multiuser-admin.txt`（权限 0600）。验收使用的 `qa_console_a`、`qa_console_b` 为隔离测试账号，不是产品默认账号。你可自行注册新账号进行手工测试。

## 手工测试

1. 注册 A，记录“我最喜欢无糖茉莉花茶”，等处理完成，发送 `/ask 我喜欢什么饮料？`。
2. 打开“四类记忆库”，检查偏好以及“原文与版本对比”。刷新页面，确认仍登录。
3. 用“我的账号 → 退出登录”退出，再注册 B。B 的历史和记忆库应为空；相同问题不能召回 A 的信息。
4. 两个账号同时测试时，使用普通窗口和无痕窗口或不同浏览器。普通标签页共享登录 Cookie，切换账号会通知其他标签页刷新。
5. “我的账号”中修改密码后，所有设备的旧会话应失效，新密码可重新登录。
6. 登录 `local_admin`，发送消息并检查完整执行图；管理员只查看自己账号的记录。

## 配置与迁移

新默认是账号模式，旧的共享令牌不会在账号模式中生效。测试环境已运行 Alembic `20261025_0017`，仅增加账号和会话表。

```dotenv
SUIXINJI_CONSOLE_ENABLED=1
SUIXINJI_CONSOLE_AUTH_MODE=accounts
# 本地手工测试开放；正式环境默认不开放注册。
SUIXINJI_CONSOLE_REGISTRATION_ENABLED=true
# 仅本地 HTTP 使用 false；正式 HTTPS 保持默认 true。
SUIXINJI_CONSOLE_COOKIE_SECURE=false
```

配置使用 `environments/test/.env`，与正式数据库隔离；保持自动记忆整理关闭。测试服务管理：

```bash
.venv/bin/python scripts/local_services.py status test
.venv/bin/python scripts/local_services.py stop test
.venv/bin/python scripts/local_services.py start test
```

普通新环境需先使用目标环境配置执行 `alembic upgrade head`；不要把测试数据库配置用于正式环境。管理员创建和忘记密码后重置均通过本机命令完成，密码交互输入，不放在命令行参数中：

```bash
.venv/bin/python scripts/console_account.py create your_admin --admin --alias 管理员 --env-file environments/test/.env
.venv/bin/python scripts/console_account.py reset-password username --env-file environments/test/.env
```

目前没有邮件找回或邀请系统。只有明确设为 `SUIXINJI_CONSOLE_AUTH_MODE=token` 时才启用旧管理员令牌流程；该模式不提供多用户隔离，不用于公开多用户服务。

## 会话与权限

密码使用独立盐的 scrypt 哈希；数据库只保存会话令牌的 SHA-256 摘要。Cookie 为 HttpOnly、SameSite=Strict，默认 Secure，固定 7 天有效；退出撤销当前会话，改密码撤销全部会话。修改请求要求同源页面发送自定义请求头；Redis 对登录/注册按客户端地址和用户名限流。不要配置允许任意来源携带凭证的 CORS。

账号模式的控制台关闭旧 `/v1/commands` 测试入口；普通用户的运行接口只返回消息、回答和处理状态，不返回原始模型调用、后台任务参数或内部错误。所有私人 API 禁止缓存。

## 已验证

- 全量自动测试：887 passed / 38 skipped；跳过项不算通过。
- 本地真实 PostgreSQL / Redis：A 的消息、记忆、版本接口对 B 返回 404。
- 真实模型：A 可召回自己记录的茉莉花茶偏好，B 返回无相关记录。
- 浏览器：注册、会话恢复、记录处理、历史回答和私人记忆库。
- 本次修改的 Python 文件 Ruff 检查及前端 JavaScript 语法检查通过；全库 Ruff 仍报告原有未跟踪 `eval/` 脚本的 14 项问题，本次未改动这些脚本。

这轮不迁移旧账号数据，不扩展全站管理员权限，不代表服务器部署验收。
