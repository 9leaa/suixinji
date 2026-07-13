import json

input_file = "/home/zcj/suixinji/data/cache/g_oc_503174b74067890b6439c33fe1e915d8.jsonl"
output_file = "/home/zcj/suixinji/data/cache/g_oc_503174b74067890b6439c33fe1e915d8_pending.jsonl"


def update_status(obj):
    """
    递归修改所有 status 字段：
    processed -> pending
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