# 本机统一代码与双环境

代码统一在 `/Users/zhangchengjie/Projects/suixinji/server`，使用 `main` 分支。
原 `main-integration`、`fact-memory-server` 已删除；历史分支仍在 Git 中。

| 环境 | 配置 | 运行数据 | 服务 |
|---|---|---|---|
| 正式 | `environments/production/.env` | `environments/production/data/` | API 18000、飞书、分布式 Worker |
| 测试 | `environments/test/.env` | `environments/test/data/` | 控制台 18080、adaptive Worker |

两套环境共享代码及 `.venv`，使用各自原有数据库、Redis 配置和凭证。启动器将工作目录切到所选环境，清除从另一环境继承的配置，并禁止隐式加载根目录 `.env`。`environments/` 不进入 Git。

```bash
cd /Users/zhangchengjie/Projects/suixinji/server
.venv/bin/python scripts/local_services.py status all
.venv/bin/python scripts/local_services.py stop test
.venv/bin/python scripts/local_services.py start test
# production 代表正式环境，all 代表两套环境。
```

旧 `scripts/start_distributed.sh`、`stop_distributed.sh`、`status_distributed.sh` 在本机自动操作正式环境。测试控制台用上述 test 入口。
普通 checkout 未配置 environments 时，旧脚本仍保持原行为。
不要直接在代码根目录启动生产 Worker；需要使用明确的环境入口，避免相对路径写入错误数据目录。
更改共享源码会影响两套环境，下次重启均加载最新代码。

自动记忆整理在两套配置中仍为 false，按用户要求只保留手动整理。

迁移核对：原配置 SHA-256 未变；正式库笔记 1269、记忆 6049，测试库笔记 1673、记忆 283；仅为迁移时的数量快照。主线回归 871 passed / 38 skipped，另有 3 项环境隔离测试通过。控制台真实 `/status` 请求完成、显示结果；不代表飞书收发或模型抽取的全链路重新验收。
