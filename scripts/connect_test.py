"""文件作用：外部连接连通性辅助脚本。

项目关系：本文件依赖 无直接本地模块依赖；被 暂无静态导入方或仅作为入口脚本执行。
"""


import os
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

base_url = os.getenv("OPENAI_BASE_URL")
api_key = os.getenv("OPENAI_API_KEY")
model = os.getenv("OPENAI_MODEL")

print("OPENAI_BASE_URL =", base_url)
print("OPENAI_MODEL =", model)
print("OPENAI_API_KEY exists =", bool(api_key))

client = OpenAI(
    api_key=api_key,
    base_url=base_url,
)

response = client.chat.completions.create(
    model=model,
    messages=[
        {
            "role": "user",
            "content": "请只回复：Python LLM 测试成功",
        }
    ],
    temperature=0,
)

print(response.choices[0].message.content)
