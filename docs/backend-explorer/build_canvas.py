"""Build a local backend canvas using the user-provided interaction reference."""

from pathlib import Path
import json
import re

HERE = Path(__file__).parent
reference = Path("/Users/zhangchengjie/Downloads/数字人系统_交互画布.html").read_text()
S = json.loads((HERE / "snapshot.json").read_text())
nodes = {}


def node(id, name, kind, simple, tech, where, explain, path, evidence="已核对当前项目源码；运行状态见带时间戳的快照。"):
    nodes[id] = dict(name=name, kind=kind, simple=simple, tech=tech, where=where, explain=explain, path=path, evidence=evidence)


node(
    "user",
    "飞书用户",
    "消息来源",
    "随手记录，查询历史",
    "飞书文本消息 / 命令",
    "飞书客户端",
    "项目当前只处理文本；用户消息进入长连接接收入口。",
    "bot/feishu_bot.py",
    "尚未完成本次实际消息输入到飞书回复的端到端验收。",
)
node(
    "receiver",
    "Receiver",
    "入口进程",
    "接收消息，避免重复",
    "InboxCommand / Idempotency",
    "本机 Python 进程",
    "Redis 幂等键快速拦截，PostgreSQL 持久身份兜底去重。将输入、任务与发布意图一起提交。",
    "apps/receiver.py · repositories/postgres/dispatch.py",
)
node(
    "api",
    "FastAPI",
    "HTTP 接口",
    "检查健康，受控接入",
    "/health / token-protected API",
    "127.0.0.1:18000",
    "健康接口只证明 API 可响应；业务测试接口需要开关和鉴权。",
    "apps/api.py",
    "/health 已返回 ok；本图不会发送业务请求。",
)
node(
    "pg",
    "PostgreSQL",
    "持久事实来源",
    "保存记录和任务账本",
    "PostgreSQL + pgvector + pg_trgm",
    "127.0.0.1:15432 / suixinji_v2",
    "31 张业务表保存租户、原始消息、任务、笔记、记忆版本、发送结果和审计。数据库提供事务、索引、租约与状态校验。",
    "infrastructure/schema.py · alembic/versions/",
    "已直接采样表、列、外键和索引；迁移版本 " + S["revision"] + "。",
)
node(
    "redis",
    "Redis",
    "快速协调",
    "派工、去重、缓存、锁",
    "Redis Streams + TTL + Lua",
    "127.0.0.1:16379 / DB 0",
    "Redis 不保存全部业务事实；主要用于通知队列、协调并发和短期状态。当前 AOF 开启，但这不等于高可用集群。",
    "infrastructure/redis_keys.py · runtime/streams/client.py",
)
node(
    "relay",
    "Outbox Relay",
    "跨系统桥梁",
    "把数据库工单送到队列",
    "Leased Outbox → XADD",
    "本机 Python 进程",
    "认领 Outbox，发布 task_id 通知后标记 published。两系统无共同事务，崩溃窗口可能导致重复通知；失败退避重试。",
    "apps/outbox_relay.py · repositories/postgres/outbox.py",
)
node(
    "scheduler",
    "Scheduler",
    "周期调度",
    "到时间派发总结和整合",
    "Leader TTL / due retries",
    "本机 Python 进程",
    "拿到 Redis 选主锁后扫描总结、记忆整合和画像重建；扫描到期 retry 并重新派发。",
    "apps/scheduler.py",
)
roles = {
    "ingest": ("记录 Worker", "保存原始 Note，派发后续工作", "process_record / Note watermark"),
    "memory": ("记忆 Worker", "抽取、审理和演化记忆", "Candidate / Decision / Version"),
    "query": ("查询 Worker", "检索证据，生成回答", "Hybrid retrieval / consistency"),
    "summary": ("总结 Worker", "生成周期回顾", "Summary / delivery task"),
    "embedding": ("向量 Worker", "补齐记忆的语义向量", "content_hash / embedding version"),
    "projection": ("画像 Worker", "更新可重建的画像", "Dirty facet / debounce"),
    "enrichment": ("丰富 Worker", "异步丰富笔记", "Note enrichment"),
    "delivery": ("发送 Worker", "把结果发回飞书", "Reservation / sent / failed / unknown"),
}
for id, (name, simple, tech) in roles.items():
    typ = {"embedding": "memory_embedding", "projection": "semantic_profile_projection"}.get(id, id)
    node(
        id,
        name,
        "worker-" + typ,
        simple,
        tech,
        "本机独立 Python 进程",
        simple + "。消费 " + typ + " Stream，先数据库认领并续租，再执行业务；提交状态后确认旧通知。",
        "apps/handlers.py · runtime/streams/worker.py",
    )
node(
    "model",
    "LLM / Embedding",
    "外部能力",
    "理解文本、生成向量",
    "OpenAI-compatible API",
    "DeepSeek / DashScope",
    "模型与向量服务独立于 PostgreSQL / Redis；并发、费用预算和响应延迟会影响吞吐。本图不触发模型请求。",
    "core/llm_client.py",
)
node(
    "inbox",
    "Inbox",
    "数据库 · 入口",
    "原始消息与先后顺序",
    "inbox_messages / sequence_no",
    "PostgreSQL 业务表",
    "入口保存原始文本和 Note / Memory 阶段状态；sequence_no 与双水位用于前序依赖。",
    "repositories/postgres/dispatch.py",
)
node(
    "tasks",
    "Tasks / Attempts",
    "数据库 · 工单",
    "谁在做，做到了哪一步",
    "tasks + task_attempts / fencing",
    "PostgreSQL 业务表",
    "状态、failure_count、defer_count、lease_token、claim_version 和 attempt 记录帮助认领、重试、接管与审计。",
    "repositories/postgres/tasks.py",
)
node(
    "outbox",
    "Outbox",
    "数据库 · 待通知",
    "数据库里的待发布清单",
    "outbox_events / transactional outbox",
    "PostgreSQL 业务表",
    "业务写入与发布意图同事务提交。已 published 的事件不会仅因 Redis 数据丢失自动全部重发，需要恢复核对。",
    "repositories/postgres/outbox.py",
)
node(
    "notes",
    "Note 数据域",
    "数据库 · 证据",
    "保留原始记录和语义向量",
    "notes / tags / relations / embeddings",
    "PostgreSQL · 4 张表",
    "notes 是原始证据；note_tags、note_relations、note_embeddings 补充标签、关系与向量。",
    "infrastructure/schema.py · repositories/postgres/notes.py",
)
node(
    "memories",
    "Memory 数据域",
    "数据库 · 长期记忆",
    "有效记忆、决策和版本来源",
    "Candidate → Decision → Version",
    "PostgreSQL · 记忆相关表",
    "记录候选、审理、关系、版本、来源、向量和抽取状态；semantic_profile_projections 可从记忆重建。",
    "infrastructure/schema.py · repositories/postgres/memory.py",
)
node(
    "audit",
    "审计与发送数据",
    "数据库 · 运行账本",
    "查为什么这么答、是否发出",
    "agent_runs / steps / usage / deliveries",
    "PostgreSQL 业务表",
    "保存运行、步骤、模型用量和发送尝试；发送超时为 unknown，避免盲目重复发送。",
    "core/observability.py · runtime/delivery_store.py",
)
node(
    "streams",
    "8 类 Streams",
    "Redis · 任务通知",
    "不同工种排不同队",
    "XADD / XREADGROUP / XACK",
    S["redis"]["prefix"] + ":stream:{task_type}",
    "消息携带 event_id、task_id、task_type 和 attempt。XACK 清除 Pending，不删除队列历史；Stream 采用近似 MAXLEN。",
    "runtime/streams/client.py",
)
node(
    "pending",
    "Pending / 接管",
    "Redis · 故障恢复",
    "未签收工单可以被接管",
    "PEL / XAUTOCLAIM",
    "消费组 Pending Entries List",
    "达到空闲阈值可接管通知；是否有权执行仍要查数据库租约。不能把接管通知等同获得业务所有权。",
    "runtime/streams/client.py · runtime/streams/worker.py",
)
node(
    "locks",
    "锁与去重",
    "Redis · 协调",
    "别重复接单，别同时乱改",
    "SET NX PX / token / Lua",
    "idem:* / lock:*",
    "入口去重有 TTL；锁通过 token 续租与释放。关键锁可在 Redis 获取失败时退到 PostgreSQL advisory lock，队列本身不会整体降级。",
    "infrastructure/redis_lock.py · redis_idempotency.py",
)
node(
    "cache",
    "缓存与会话",
    "Redis · 临时状态",
    "少重复查，记住短期上下文",
    "Versioned cache / TTL / Session",
    "cache:* / cachever:* / session:*",
    "搜索缓存由版本隔离旧结果；Embedding 缓存按模型和文本哈希复用；会话带 TTL。",
    "infrastructure/redis_cache.py · redis_session.py",
)
node(
    "limits",
    "限流与访问缓冲",
    "Redis · 资源控制",
    "控制接活速度，攒小更新",
    "Rate / concurrency / access counts",
    "rate:* / concurrency:* / memory:access:*",
    "用户请求限流、模型并发与 Token 预算；记忆访问计数与最近访问时间缓冲后刷回数据库。",
    "infrastructure/redis_rate_limit.py · memory/repository.py",
)
node(
    "lease",
    "数据库租约",
    "分布式 · 所有权",
    "工单有临时负责人",
    "lease_token + claim_version",
    "tasks 表 + 续租线程",
    "完成、失败、延期和续租都校验所有权；过期接管产生新版本，阻止旧 Worker 标记过时任务为完成。",
    "repositories/postgres/tasks.py",
)
node(
    "watermark",
    "Note / Memory 水位",
    "分布式 · 一致性",
    "等前面处理完，再回答",
    "required_watermark / blocked",
    "spaces + inbox_messages",
    "查询根据问题选择 weak / note / memory；连续阶段水位不足则 blocked，前序完成或终结后解阻塞。阶段终结不保证记忆抽取成功。",
    "runtime/consistency.py · repositories/postgres/dispatch.py",
)
node(
    "retry",
    "重试与死信",
    "分布式 · 恢复",
    "失败稍后再做，耗尽就停",
    "Exponential backoff / dead_letter",
    "PostgreSQL retry + Redis deadletter",
    "普通失败增加 failure_count；RetryLater 增加 defer_count。状态持久化后 ACK 旧通知，到期通过新 Outbox 重投。",
    "repositories/postgres/tasks.py · runtime/streams/worker.py",
)
node(
    "volume",
    "持久数据卷",
    "本机 · Docker",
    "重建容器保留数据",
    "Docker named volumes / Redis AOF",
    "suixinji-local-postgres-data / suixinji-local-redis-data",
    "数据库与 Redis 分别使用命名卷；应用日志在 data/logs。数据卷不等于异地备份。",
    "Docker 实际启动配置 · data/logs/",
    "本次启动创建独立 15432 / 16379 实例，未导入远程历史数据。",
)
edges = []


def edge(a, b, label, conditional=False):
    edges.append([a, b, label, conditional])


for a, b, label, conditional in [
    ("user", "receiver", "飞书长连接", False),
    ("api", "receiver", "受控接入", True),
    ("receiver", "pg", "事务写入", False),
    ("receiver", "locks", "入口去重", False),
    ("pg", "relay", "认领待发布事件", False),
    ("relay", "streams", "XADD 通知", False),
    ("scheduler", "pg", "周期任务 / 到期重试", False),
    ("scheduler", "locks", "调度选主", False),
    ("redis", "streams", "队列能力", False),
    ("redis", "pending", "未确认通知", False),
    ("redis", "locks", "锁 / 去重", False),
    ("redis", "cache", "短期结果", False),
    ("redis", "limits", "资源预算", False),
    ("pg", "inbox", "入口证据", False),
    ("pg", "tasks", "任务事实", False),
    ("pg", "outbox", "发布意图", False),
    ("pg", "notes", "笔记与向量", False),
    ("pg", "memories", "记忆演化", False),
    ("pg", "audit", "运行 / 发送审计", False),
    ("pg", "watermark", "连续阶段进度", False),
    ("pg", "volume", "数据库持久化", False),
    ("redis", "volume", "AOF 持久化", False),
    ("inbox", "tasks", "同事务创建工单", False),
    ("tasks", "outbox", "同事务创建通知", False),
    ("outbox", "relay", "发布认领", False),
    ("ingest", "notes", "保存 Note", False),
    ("ingest", "memory", "按需派发抽取", True),
    ("ingest", "enrichment", "异步丰富", False),
    ("memory", "memories", "审理 / 版本更新", False),
    ("memory", "embedding", "补齐记忆向量", True),
    ("memory", "projection", "标记画像更新", True),
    ("embedding", "memories", "保存向量状态", False),
    ("projection", "memories", "刷新画像投影", False),
    ("query", "notes", "检索原始证据", False),
    ("query", "memories", "检索有效记忆", False),
    ("query", "cache", "版本化结果缓存", False),
    ("query", "watermark", "查询一致性", False),
    ("query", "model", "组织回答", False),
    ("summary", "model", "生成回顾", False),
    ("ingest", "model", "分类 / 向量", True),
    ("memory", "model", "候选抽取 / 审理", True),
    ("embedding", "model", "生成向量", False),
    ("query", "delivery", "派发回答", False),
    ("summary", "delivery", "派发总结", False),
    ("delivery", "audit", "预留 / 记录结果", False),
    ("delivery", "user", "飞书 API 发送", False),
    ("pending", "lease", "接管后核对所有权", False),
    ("lease", "tasks", "续租 / 完成校验", False),
    ("retry", "outbox", "到期重新派发", False),
    ("retry", "streams", "死信记录", True),
    ("enrichment", "notes", "后台补充", False),
]:
    edge(a, b, label, conditional)
for w in roles:
    edge("streams", w, "消费 " + ({"embedding": "memory_embedding", "projection": "semantic_profile_projection"}.get(w, w)))
    edge(w, "lease", "数据库认领 / 续租")
flows = {}
scenes = {}


def scene(key, label, points, groups, steps):
    scenes[key] = {"points": points, "groups": groups}
    flows[key] = {
        "label": label,
        "context": "源码逻辑演示；不会触发真实业务。",
        "steps": [{"ids": ids, "title": title, "text": text, "state": state} for ids, title, text, state in steps],
    }


scene(
    "overview",
    "整体后端",
    {
        "user": [50, 90],
        "api": [50, 340],
        "receiver": [410, 190],
        "scheduler": [410, 560],
        "pg": [800, 360],
        "relay": [1200, 190],
        "streams": [1570, 190],
        "redis": [1570, 540],
        "ingest": [1960, 70],
        "memory": [2320, 70],
        "query": [1960, 330],
        "summary": [2320, 330],
        "embedding": [1960, 590],
        "projection": [2320, 590],
        "enrichment": [1960, 850],
        "delivery": [2320, 850],
        "model": [2740, 340],
        "volume": [800, 840],
    },
    [
        ["01 接入", ["user", "api", "receiver"], "business"],
        ["02 调度", ["scheduler", "relay"], "business"],
        ["03 持久事实", ["pg", "volume"], "storage"],
        ["04 队列与协调", ["streams", "redis"], "storage"],
        ["05 独立 Worker", ["ingest", "memory", "query", "summary", "embedding", "projection", "enrichment", "delivery"], "business"],
        ["06 外部模型", ["model"], "capability"],
    ],
    [],
)
recordsteps = [
    (
        ["user", "receiver"],
        "用户发来记录",
        "将文本整理为 InboxCommand，并按消息身份去重。",
        {"DB": "未提交", "Redis": "检查幂等键", "用户": "消息到达入口"},
    ),
    (
        ["receiver", "pg", "inbox", "tasks", "outbox"],
        "同事务留证据与工单",
        "原始消息、任务和待发布事件同事务提交。",
        {"DB": "inbox=pending; task=queued; outbox=pending", "Redis": "尚无对应任务通知", "用户": "接收确认不等于完成整理"},
    ),
    (
        ["outbox", "relay", "streams"],
        "发布任务通知",
        "Relay 认领 Outbox 后 XADD，成功后标记 published。",
        {"DB": "outbox=published", "Redis": "ingest Stream 新通知", "用户": "等待后台处理"},
    ),
    (
        ["streams", "ingest", "lease", "tasks"],
        "数据库认领",
        "XREADGROUP 后 claim_task，设置租约与 claim_version。",
        {"DB": "task=running; lease 有效", "Redis": "通知进入 Pending", "用户": "处理中"},
    ),
    (
        ["ingest", "notes", "memory"],
        "保存 Note 并派发记忆",
        "原始证据保存后，再由独立 memory 任务抽取与审理。",
        {"DB": "Note 已保存；Memory 任务独立执行", "Redis": "memory / enrichment 通知独立派发", "用户": "记录与记忆的完成时刻不同"},
    ),
    (
        ["memory", "memories", "embedding", "projection"],
        "形成记忆与后续投影",
        "版本化记忆保存来源；向量与画像独立更新。",
        {"DB": "Memory / version / sources 更新", "Redis": "向量和画像任务可能仍处理中", "用户": "异步后续阶段存在时间窗口"},
    ),
    (
        ["ingest", "lease", "tasks", "streams"],
        "提交完成后 ACK",
        "有效所有者提交任务 completed，XACK 清除旧通知的 Pending。",
        {"DB": "原 ingest task=completed", "Redis": "主通知 ACK；后续任务各自 ACK", "用户": "不把 ACK 当作所有业务已结束"},
    ),
]
points = {
    "user": [40, 250],
    "receiver": [390, 250],
    "pg": [780, 620],
    "inbox": [390, 620],
    "tasks": [1170, 620],
    "outbox": [780, 250],
    "relay": [1170, 250],
    "streams": [1560, 250],
    "ingest": [1950, 250],
    "lease": [1560, 620],
    "notes": [1950, 620],
    "memory": [2340, 250],
    "memories": [2730, 250],
    "embedding": [2730, 620],
    "projection": [2340, 620],
}
scene(
    "record",
    "记录与记忆",
    points,
    [
        ["01 接收", ["user", "receiver"], "business"],
        ["02 原子保存", ["pg", "inbox", "tasks", "outbox"], "storage"],
        ["03 通知与执行", ["relay", "streams", "ingest", "lease"], "business"],
        ["04 证据与演化", ["notes", "memory", "memories", "embedding", "projection"], "storage"],
    ],
    recordsteps,
)
scene(
    "query",
    "查询与发送",
    {
        "user": [40, 300],
        "receiver": [400, 300],
        "pg": [790, 650],
        "watermark": [790, 300],
        "streams": [1180, 300],
        "query": [1570, 300],
        "notes": [1570, 50],
        "memories": [1960, 50],
        "cache": [1180, 650],
        "model": [1960, 300],
        "delivery": [2350, 300],
        "audit": [2350, 650],
    },
    [
        ["01 查询入口", ["user", "receiver"], "business"],
        ["02 前序一致性", ["pg", "watermark"], "storage"],
        ["03 任务通知", ["streams", "cache"], "storage"],
        ["04 混合检索", ["query", "notes", "memories", "model"], "capability"],
        ["05 发送与审计", ["delivery", "audit"], "business"],
    ],
    [
        (
            ["user", "receiver", "watermark"],
            "问题选择一致性",
            "偏好问题通常等待 memory 水位，最近记录通常等待 note 水位，其他可 weak。",
            {"DB": "required_watermark 与 spaces 比较", "Redis": "blocked 任务暂不发布", "用户": "可能等待前序整理"},
        ),
        (
            ["pg", "watermark", "streams"],
            "解阻塞后派工",
            "前序阶段推进连续水位，符合条件的任务 queued 并产生 Outbox。",
            {"DB": "blocked → queued", "Redis": "query Stream 有新通知", "用户": "开始查询"},
        ),
        (
            ["streams", "query", "notes", "memories", "cache"],
            "混合检索",
            "检索 Note 和 active Memory，缓存按空间版本隔离。",
            {"DB": "读取证据及来源", "Redis": "可能命中版本化缓存", "用户": "等待答案"},
        ),
        (
            ["query", "model"],
            "组织答案",
            "调用模型基于实际选中的证据回答，模型失败不等于数据库故障。",
            {"DB": "记录 Agent 步骤 / 用量", "Redis": "并发与预算约束", "用户": "答案仍在生成"},
        ),
        (
            ["query", "delivery", "audit"],
            "派发并预留发送",
            "delivery_key 预留防止常规重复，发送记录分 sent / failed / unknown。",
            {"DB": "delivery=reserved", "Redis": "delivery 任务通知", "用户": "尚未确认送达"},
        ),
        (
            ["delivery", "user", "audit"],
            "发送结果留痕",
            "成功记 sent；超时记 unknown，不盲目重复发送。",
            {"DB": "sent / failed / unknown", "Redis": "保存任务结果后 ACK", "用户": "超时只能确认不确定，不能假定成功"},
        ),
    ],
)
scene(
    "recovery",
    "崩溃与重试",
    {
        "pg": [40, 560],
        "outbox": [390, 250],
        "relay": [780, 250],
        "streams": [1170, 250],
        "ingest": [1560, 250],
        "pending": [1170, 600],
        "lease": [1560, 600],
        "tasks": [1950, 600],
        "retry": [1950, 250],
    },
    [
        ["01 持久发布意图", ["pg", "outbox"], "storage"],
        ["02 通知投递", ["relay", "streams"], "business"],
        ["03 Worker 与接管", ["ingest", "pending", "lease"], "business"],
        ["04 状态与重试", ["tasks", "retry"], "optional"],
    ],
    [
        (
            ["pg", "outbox", "relay", "streams"],
            "投递失败时留住工单",
            "XADD 失败后 Outbox retry；Redis 恢复再尝试。",
            {"DB": "outbox=retry; task=queued", "Redis": "暂时未收到通知", "所有权": "尚未认领"},
        ),
        (
            ["streams", "ingest", "lease", "tasks"],
            "Worker 正在执行",
            "读取通知并认领数据库任务，周期续租。",
            {"DB": "task=running; claim_version=1", "Redis": "Pending=1（示意）", "所有权": "Worker A 有效租约"},
        ),
        (
            ["ingest", "pending", "lease"],
            "Worker 中途退出",
            "通知未 ACK，租约仍要等到过期；超时接管通知不等于立即有执行权。",
            {"DB": "running; lease 等待到期", "Redis": "Pending 保留", "所有权": "不能越过有效租约"},
        ),
        (
            ["pending", "lease", "tasks"],
            "接管并排除旧结果",
            "XAUTOCLAIM 回收通知；有效条件下数据库重新认领，claim_version 增加。",
            {"DB": "claim_version=2（示意）", "Redis": "Pending 归 Worker B", "所有权": "旧 Worker A 完成校验失败"},
        ),
        (
            ["ingest", "retry", "tasks"],
            "异常后保存重试状态",
            "普通失败计数加一、指数退避；RetryLater 是延期，不增加普通失败次数。",
            {"DB": "task=retry; next_retry_at", "Redis": "旧通知 ACK", "所有权": "当前认领结束"},
        ),
        (
            ["retry", "outbox", "relay", "streams"],
            "到期产生新通知",
            "到期扫描创建新 Outbox 再派发；失败耗尽进入 dead_letter。",
            {"DB": "queued / dead_letter", "Redis": "新通知 / deadletter", "所有权": "下一次重新认领"},
        ),
    ],
)
scene(
    "redisview",
    "Redis 内部",
    {
        "receiver": [40, 240],
        "scheduler": [40, 530],
        "redis": [440, 400],
        "streams": [900, 120],
        "pending": [1300, 120],
        "locks": [900, 420],
        "cache": [1300, 420],
        "limits": [900, 720],
        "query": [1720, 420],
        "lease": [1720, 120],
        "pg": [1720, 720],
    },
    [
        ["01 使用方", ["receiver", "scheduler"], "business"],
        ["02 Redis 实例", ["redis"], "storage"],
        ["03 队列与恢复", ["streams", "pending"], "storage"],
        ["04 协调与临时状态", ["locks", "cache", "limits"], "storage"],
        ["05 业务事实校验", ["query", "lease", "pg"], "business"],
    ],
    [
        (
            ["redis", "streams", "pending"],
            "Stream 与 Pending",
            "Length、Pending、Lag 是不同指标；ACK 不删除消息历史。",
            {"Length": "保留消息总数", "Pending": "已投递未确认数", "Lag": "尚未投递数"},
        ),
        (
            ["receiver", "locks", "scheduler"],
            "去重与锁",
            "消息幂等键、空间锁和 Scheduler 选主各自有键和 TTL。",
            {"键": "idem / lock:space / lock:scheduler", "持久兜底": "数据库唯一约束", "边界": "锁失效不代表队列可整体降级"},
        ),
        (
            ["redis", "cache", "query"],
            "缓存与会话",
            "版本化检索缓存、Embedding 哈希缓存和短期会话状态。",
            {"检索缓存": "空间版本 + query hash", "Embedding": "model + text hash", "会话": "tenant + user + TTL"},
        ),
        (
            ["redis", "limits", "pg"],
            "限流与访问缓冲",
            "模型并发 / Token 预算控制资源；访问计数缓冲后批量刷回 DB。",
            {"资源": "请求 / 模型并发 / Token", "缓冲": "访问次数 / 最近时间", "写回": "PostgreSQL"},
        ),
        (
            ["pending", "lease", "pg"],
            "通知与事实分离",
            "Redis 可重复通知；业务是否已完成、谁拥有任务，以数据库状态校验为准。",
            {"通知": "Redis Streams", "事实": "PostgreSQL tasks", "一致性": "至少一次通知 + 幂等 / fencing"},
        ),
    ],
)
scene(
    "database",
    "数据库内部",
    {
        "pg": [600, 410],
        "inbox": [40, 100],
        "tasks": [410, 100],
        "outbox": [780, 100],
        "watermark": [1150, 100],
        "notes": [40, 730],
        "memories": [450, 730],
        "audit": [860, 730],
        "volume": [1270, 730],
    },
    [
        ["01 事务处理", ["inbox", "tasks", "outbox", "watermark"], "storage"],
        ["02 持久中心", ["pg"], "storage"],
        ["03 业务数据域", ["notes", "memories", "audit"], "storage"],
        ["04 物理持久化", ["volume"], "storage"],
    ],
    [
        (
            ["pg", "inbox", "tasks", "outbox"],
            "事务保存链",
            "入口证据、工作状态和发布意图一起提交，避免成功入库但没有派工记录。",
            {"入口": "inbox_messages", "任务": "tasks / task_attempts", "发布": "outbox_events"},
        ),
        (
            ["pg", "notes", "memories"],
            "证据与记忆分层",
            "Note 保留原始信息，Memory 可演化；版本与来源支持追溯。",
            {
                "证据": "notes / note_embeddings",
                "演化": "candidates / decisions / relations",
                "来源": "sources / versions / version_sources",
            },
        ),
        (
            ["pg", "watermark"],
            "空间和连续进度",
            "spaces 保存 Note / Memory 两条水位；租户和成员关系约束数据归属。",
            {"隔离": "tenants / users / spaces / space_members", "进度": "note_watermark / memory_watermark", "等待": "blocked → queued"},
        ),
        (
            ["pg", "audit"],
            "发送和运行审计",
            "查任务为何失败、模型调用了多少、消息是否送达。",
            {
                "运行": "agent_runs / agent_steps / llm_usage",
                "发送": "deliveries / delivery_attempts",
                "总结": "summary_runs / subscriptions",
            },
        ),
        (
            ["pg", "volume"],
            "数据独立于容器",
            "命名卷保留数据库文件；卷不是备份。全部 31 张表可在“数据与状态”查看。",
            {"扩展": "vector / pg_trgm", "迁移": S["revision"], "边界": "本机新建库，未导入远程历史"},
        ),
    ],
)
# Lay out each task stream beside its consumer, using two semantic groups.
for worker in roles:
    typ = {"embedding": "memory_embedding", "projection": "semantic_profile_projection"}.get(worker, worker)
    q = "queue_" + worker
    node(
        q,
        typ + " 队列",
        "Redis Stream",
        "只分发 " + typ + " 任务",
        "XREADGROUP / Consumer Group",
        S["redis"]["prefix"] + ":stream:" + typ,
        "该队列与对应 Worker 一一配对；通知只携带任务身份，业务事实仍在 PostgreSQL。",
        "runtime/streams/client.py",
    )
    edge("streams", q, "按类型路由")
    edge(q, worker, "消费任务")
pts = scenes["overview"]["points"]
for idx, worker in enumerate(["ingest", "memory", "query", "summary", "embedding", "projection", "enrichment", "delivery"]):
    col = 0 if idx < 4 else 1
    row = idx % 4
    pts["queue_" + worker] = [1960 + col * 850, 80 + row * 250]
    pts[worker] = [2320 + col * 850, 80 + row * 250]
pts["model"] = [3610, 420]
groups = scenes["overview"]["groups"]
groups[:] = [g for g in groups if g[0] != "05 独立 Worker" and g[0] != "06 外部模型"]
groups.extend(
    [
        ["05 主业务 · 队列 → Worker", [v for w in ["ingest", "memory", "query", "summary"] for v in ["queue_" + w, w]], "business"],
        [
            "06 后台补充 · 队列 → Worker",
            [v for w in ["embedding", "projection", "enrichment", "delivery"] for v in ["queue_" + w, w]],
            "business",
        ],
        ["07 外部模型", ["model"], "capability"],
    ]
)
def js(v):
    return json.dumps(v, ensure_ascii=False)
h = reference
h = re.sub(
    r"const nodes=\{.*?\nconst \$=id=>",
    lambda _: "const nodes=" + js(nodes) + ";\nconst edges=" + js(edges) + ";\nconst flows=" + js(flows) + ";\nconst $=id=>",
    h,
    flags=re.S,
)
h = re.sub(r"const sceneDefs=\{.*?\nconst dimensions=", lambda _: "const sceneDefs=" + js(scenes) + ";\nconst dimensions=", h, flags=re.S)
h = h.replace("id==='d'", "id==='pg'").replace("selected='d'", "selected='pg'").replace("view==='overview'?'d'", "view==='overview'?'pg'")
h = h.replace("'digital-human-interaction-canvas-v3'", "'suixinji-backend-canvas-v3'")
h = h.replace(
    "const storageIds=new Set(['pg','redis','portrait','report','local']);",
    "const storageIds=new Set(['pg','redis','inbox','tasks','outbox','notes','memories','audit','streams','pending','cache','volume']);",
)
h = h.replace("const remoteIds=new Set(['model','video','rag','cloud','x','moderation']);", "const remoteIds=new Set(['model']);")
h = h.replace("['ssm','old','rollback']", "['retry','watermark','lease','locks','limits']").replace("['old','rollback']", "['retry']")
h = h.replace(
    "{overview:'atlas',talk:'talk',news:'news',upload:'picture',knowledge:'book',release:'release'}",
    "{overview:'atlas',record:'news',query:'talk',recovery:'reset',redisview:'redis',database:'pg'}",
)
h = h.replace("mode==='knowledge'&&step===4", "false")
h = (
    h.replace("数字人系统", "随心记后端")
    .replace("原文件将这条关系标注为条件启用或非每次调用", "项目源码中这条关系为条件启用或非每次调用")
    .replace("原文件记录的证据", "源码与采样证据")
    .replace("实线：原文件中非条件关系。虚线：原文件明确标注的条件关系。", "实线：常规组件关系。虚线：按条件触发的关系。")
    .replace("条件 / 配置 / 历史", "条件 / 所有权")
    .replace("远程能力", "外部模型")
)
h = h.replace("原文件快照 · 2026-10-14 11:15 CST · 非实时监控", "本机采样 · " + S["sampled_at"].replace("T", " ") + " · 非实时监控")
h = h.replace(
    "依据原 HTML 的节点、关系和流程说明。本页面没有重新执行线上核实，也不会调用这些服务。",
    "依据随心记项目源码与脱敏采样。流程为原理演示，不调用服务、不发送真实消息。",
)
h = h.replace("查看原文件中的部署资料和核实边界", "查看表结构、队列状态、参数和运行边界").replace("资料与边界", "数据与状态")
start = h.index('<div class="dialog" id="docs-dialog"')
end = h.index('<div class="dialog" id="help-dialog"')
dialog = (
    """<div class="dialog" id="docs-dialog" hidden role="dialog" aria-modal="true" aria-labelledby="docs-title"><div class="dialog-panel"><div class="dialog-header"><div><div class="eyebrow">DATABASE / STREAMS / PROCESSES</div><h2 id="docs-title">数据与状态 · 全部表与任务队列</h2></div><button class="close" data-close-dialog aria-label="关闭资料"><span data-icon="close"></span></button></div><div class="dialog-body"><div class="document-note" id="data-note"></div><div class="intro"><div class="finding"><b>单机多进程</b><span>12 个应用角色 + PostgreSQL + Redis；不是已验证多机高可用。</span></div><div class="finding"><b>31 张业务表</b><span>可展开查看全部列、外键和实际索引，不导出业务正文。</span></div><div class="finding"><b>独立基础设施</b><span>数据库 15432 / Redis 16379；未导入远程历史业务。</span></div></div><section class="section"><h2>运行进程</h2><div class="data-scroll"><table><thead><tr><th>角色</th><th>状态</th><th>PID</th></tr></thead><tbody id="live-services"></tbody></table></div></section><section class="section"><h2>Redis Stream 采样</h2><div class="data-scroll"><table><thead><tr><th>类型 / key</th><th>Length</th><th>Pending</th><th>Lag</th></tr></thead><tbody id="live-streams"></tbody></table></div></section><section class="section"><h2>全部 31 张表</h2><label>筛选表 <input id="schema-search" type="search" placeholder="表名或中文用途"></label><div id="schema-catalog"></div></section><section class="section"><h2>当前生效配置</h2><div class="data-scroll"><table><thead><tr><th>参数</th><th>值</th></tr></thead><tbody id="live-config"></tbody></table></div></section><section class="section"><h2>边界与来源</h2><p>本图基于 apps/receiver.py、apps/handlers.py、repositories/postgres/*、runtime/streams/*、infrastructure/schema.py 与 Redis 实现。服务状态为采样结果，不是实时监控。API 健康不证明飞书收发通过。</p><p>通知为至少一次；幂等与数据库所有权校验降低重复处理，外部副作用不具备严格 exactly-once。Redis 丢失已发布通知需要恢复核对，不会自动重建所有通知。</p><p>迁移已到 """
    + S["revision"]
    + """；部分迁移索引未在 ORM metadata 声明，Alembic check 未全通过。Compose 少列两个 Worker，check_config.py 连接预算漏算一个角色；实际进程按完整启动脚本角色清单。</p><p><a href="index.html" target="_blank" rel="noopener">打开原完整说明页</a> · 重新采样：<code>.venv/bin/python docs/backend-explorer/refresh_snapshot.py</code></p></section></div></div></div>"""
)
h = h[:start] + dialog + h[end:]
h = h.replace(
    "</style>",
    """.state-dock{position:absolute;top:68px;left:20px;width:285px;background:#fffffff2;border:1px solid var(--border);border-radius:12px;padding:14px;z-index:11;box-shadow:var(--shadow)}.state-dock h3{margin:0 0 8px;font-size:12px;color:var(--teal)}.state-dock dl{margin:0;display:grid;gap:7px}.state-dock dt{font-size:10px;color:var(--muted)}.state-dock dd{font-size:12px;margin:0;overflow-wrap:anywhere}.state-dock small{color:var(--muted);font-size:10px}.data-scroll{overflow:auto}#schema-catalog details{padding:10px 0;border-bottom:1px solid var(--border)}#schema-catalog summary{cursor:pointer;font-size:13px}#schema-catalog table{font-size:11px}#schema-catalog td{overflow-wrap:anywhere}#schema-search{padding:8px 10px;border:1px solid var(--border);border-radius:6px;margin:12px}.player{width:min(850px,calc(100% - 400px));min-width:360px}.card strong{font-size:19px;white-space:normal}.card .subtitle{font-size:14px}.card.core strong{font-size:23px}.card.core .subtitle{font-size:16px}@media(max-width:760px){.state-dock{top:66px;left:12px;width:220px;padding:10px}.state-dock dl{gap:3px}.state-dock dd{font-size:10px}.state-dock dt{font-size:9px}.player{min-width:0;width:calc(100% - 24px)}.context-chip{max-width:100%}.state-dock small{display:none}}@media(prefers-reduced-motion:reduce){.edge.animate .edge-path{animation:none}}
</style>""",
)
h = h.replace(
    '<aside class="inspector"',
    '<aside class="state-dock" id="state-dock" hidden aria-live="polite"><h3>当前步骤 · 模拟状态</h3><dl id="state-values"></dl><small>仅演示逻辑，不写入真实后端</small></aside><aside class="inspector"',
)
h = h.replace(
    "const isScene=mode!=='overview';$('player').hidden=!isScene;if(!isScene)return;",
    "const isScene=mode!=='overview';$('player').hidden=!isScene;$('state-dock').hidden=!isScene;if(!isScene)return;const state=flows[mode].steps[step].state;$('state-values').innerHTML=Object.entries(state).map(([k,v])=>`<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join('');",
)
# Show the full system on first opening; selection still narrows to direct connections.
h = h.replace("step=0,scope='related'", "step=0,scope='all'")
h = h.replace("scope='related';selectedEdge=null;selected=view", "scope=view==='overview'?'all':'related';selectedEdge=null;selected=view")
# Add snapshot and data browser without business content.
h = h.replace(
    '<script id="saved-layout"',
    '<script id="snapshot" type="application/json">' + js(S).replace("<", "\\u003c") + '</script>\n<script id="saved-layout"',
)
extra = """
const S=JSON.parse($('snapshot').textContent);
document.querySelector('.navbar .snapshot').textContent='本机采样 · '+S.sampled_at+' · 非实时监控';
$('data-note').textContent='采样时间 '+S.sampled_at+'。此窗口为静态快照；刷新页面不重新查询后端。逐步状态为模拟值，与此处真实采样分开。';
$('live-services').innerHTML=S.services.map(x=>`<tr><td><code>${esc(x.role)}</code></td><td>${x.running?'运行中':'未运行'}</td><td>${x.pid??'—'}</td></tr>`).join('');
$('live-streams').innerHTML=S.streams.map(x=>`<tr><td>${esc(x.type)}<br><code>${esc(x.key)}</code></td><td>${x.length}</td><td>${x.groups.reduce((n,g)=>n+g.pending,0)}</td><td>${x.groups.map(g=>g.lag??'未知').join(' / ')}</td></tr>`).join('');
$('live-config').innerHTML=Object.entries(S.settings).map(([k,v])=>`<tr><td><code>${esc(k)}</code></td><td>${esc(v)}</td></tr>`).join('');
const DESCRIPTIONS=__DESCRIPTIONS__;
function schemaCatalog(){const q=$('schema-search').value.toLowerCase();$('schema-catalog').innerHTML=S.tables.filter(t=>(t.name+' '+DESCRIPTIONS[t.name]).toLowerCase().includes(q)).map(t=>`<details><summary>${esc(t.name)} · ${esc(DESCRIPTIONS[t.name]||'业务表')} · ${t.count} 行</summary><div class="data-scroll"><table><thead><tr><th>字段</th><th>类型</th><th>约束</th></tr></thead><tbody>${t.columns.map(c=>`<tr><td>${esc(c.name)}</td><td>${esc(c.type)}</td><td>${c.pk?'PK ':''}${c.nullable?'可空':'NOT NULL'}</td></tr>`).join('')}</tbody></table></div><p>外键：${t.fks.map(f=>esc(f.columns.join(','))+' → '+esc(f.target)).join('；')||'无声明外键'}</p><p>索引：${t.indexes.map(esc).join('；')||'无额外索引'}</p></details>`).join('')||'<p>没有匹配的表。</p>'}
$('schema-search').oninput=schemaCatalog;schemaCatalog();
"""
old = (HERE / "index.html").read_text()
descs = re.search(r"const descriptions=(\{.*?\});", old, re.S).group(1)
extra = extra.replace("__DESCRIPTIONS__", descs)
h = h.replace("loadPositions();hydrateIcons();render();", extra + "\nloadPositions();hydrateIcons();render();")
h = h.replace("随心记后端_交互画布_已保存.html", "随心记_后端交互画布_已保存.html")
h = h.replace("x-=18", "x-=36").replace("x+=18", "x+=36").replace("y-=18", "y-=36").replace("y+=18", "y+=36")
h = h.replace("const savedKey='suixinji-backend-canvas-v3'", "const savedKey='suixinji-backend-canvas-layout-v4'")
h = h.replace(
    "function visibleEdges(){",
    "function visibleEdges(){if(mode==='overview'&&scope==='all')return sceneEdges().filter(e=>!(rolesForOverview.has(e.from)&&rolesForOverview.has(e.to))&&e.to!=='model'&&!(e.from==='streams'&&rolesForOverview.has(e.to))&&!(e.from==='delivery'&&e.to==='user'));",
)
h = h.replace(
    "function visibleEdges(){",
    "const rolesForOverview=new Set(['ingest','memory','query','summary','embedding','projection','enrichment','delivery']);\nfunction visibleEdges(){",
    1,
)
h = h.replace("routedEdges.filter(e=>e.hot)", "routedEdges.filter(e=>e.hot&&!(mode==='overview'&&scope==='all'&&e.from==='streams'))")
h = h.replace("</style>", ".card strong{font-size:18px;overflow-wrap:anywhere}.card-kind{overflow-wrap:anywhere}</style>")
h = h.replace("mode==='overview'?'全部关系':'全流程'", "mode==='overview'?'主链关系':'全流程'")
h = h.replace(
    "const es=activeEdges();$('context-title')", "const es=scope==='all'&&mode==='overview'?visibleEdges():activeEdges();$('context-title')"
)
h = h.replace("离线画布 · 箭头表示调用方向，响应通常沿原连接返回", "主链按队列与 Worker 配对 · 点击 Worker 展开依赖与后续任务")
(HERE / "canvas.html").write_text(h)
print("Canvas built:", len(nodes), "nodes,", len(edges), "relations,", len(flows), "scenes")
