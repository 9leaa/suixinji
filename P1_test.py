from core.wal import create_pending_record, append_message_once
from core.worker import process_pending

space_id = "test_user"

record = create_pending_record(
    message_id="test_msg_001",
    space_id=space_id,
    text="今天看了一篇关于 RAG 语义分块的文章，感觉按标题层级切分不一定适合小说。",
    sender={"open_id": "test"},
)

append_message_once(record)

count = process_pending(space_id)
print("processed:", count)