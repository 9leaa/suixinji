# LLM Retry Breakdown

`core/llm_client.py` 现在将失败归为：

| 分类 | 含义 | 当前策略 |
|---|---|---|
| transport_timeout | 请求超时 | Memory 抽取最多重试一次 |
| connection_error | DNS、连接、TLS 或网络失败 | 记录并由上层决定是否 fallback |
| rate_limit | 429/限流 | 记录，不扩大重试风暴 |
| server_error | 5xx/网关错误 | 记录并交给上层降级 |
| invalid_json | 返回内容不是 JSON | 记录并交给上层降级 |
| truncated_response | 输出被截断 | 记录并交给上层降级 |
| empty_response | choices/content 为空 | 记录并交给上层降级 |

此前 180 条 `key_fields_and_status` strict hybrid 运行：LLM 成功 180、失败 0、传输重试 151、规则兜底 0。该数字是修复前历史运行；修复后的 LLM 运行应使用相同命令和统一报告重新采集分类计数。
