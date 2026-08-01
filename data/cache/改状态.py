"""文件作用：本地运行数据或历史调试脚本目录，通常不参与主运行时导入链。

项目关系：本文件依赖 无直接本地模块依赖；被 暂无静态导入方或仅作为入口脚本执行。
"""


import json

input_file = "/home/zcj/suixinji/data/cache/g_oc_503174b74067890b6439c33fe1e915d8.jsonl"
output_file = "/home/zcj/suixinji/data/cache/g_oc_503174b74067890b6439c33fe1e915d8_pending.jsonl"


def update_status(obj):
    """函数功能：`update_status` 负责更新 status，服务于本文件职责：本地运行数据或历史调试脚本目录，通常不参与主运行时导入链。
    传参：
        obj: obj 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "status" and value == "processed":
                obj[key] = "pending"
            else:
                update_status(value)

    elif isinstance(obj, list):
        for item in obj:
            update_status(item)


with open(input_file, "r", encoding="utf-8") as fin, \
     open(output_file, "w", encoding="utf-8") as fout:

    for line in fin:
        if not line.strip():
            continue

        data = json.loads(line)
        update_status(data)

        fout.write(json.dumps(data, ensure_ascii=False) + "\n")

print(f"处理完成，输出文件：{output_file}")