"""Isolated UI fixture: real storage/tools, deterministic model stub, never an accuracy evaluation."""
from pathlib import Path
import sys, json
from datetime import datetime, timezone
from dataclasses import asdict
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT/'.env')
from sqlalchemy.engine import make_url
from core import settings
import os
assert make_url(os.environ['DATABASE_URL']).database=='suixinji_fact_memory_test_20261020'
from memory import fact_memory as fm, fact_agent, extractor
from memory.service import process_note_memory
from storage.note_storage import save_note, NoteMetadata
from agent import query_agent as qa
from memory.trace import start_trace, add_step, finish_trace
SPACE='fmeval_ui_fixture_'+datetime.now().strftime('%Y%m%d_%H%M%S')
MODE='界面模拟验收：模型返回由固定桩提供，存储、召回与工具实际执行；不计入模型准确率。'
texts=['界面模拟验收：青石摄影社的相机存放在二楼蓝色柜子。', '青石摄影社这只柜子的钥匙由林舟保管。']
with patch.object(settings,'FACT_MEMORY_SEMANTIC_SEARCH_ENABLED',False):
    for i, text in enumerate(texts):
        note=NoteMetadata(id=SPACE+'-'+str(i),message_id=SPACE+'-'+str(i),space_id=SPACE,
                          ts=datetime.now(timezone.utc).isoformat(),title=text,tags=[],type='记录',summary=text,text=text,related=[])
        save_note(note)
        with patch.object(extractor,'complete_json',return_value={'candidates':[{'memory_type':'semantic','content':text,'evidence_span':text,'entities':['青石摄影社'],'should_store':True}]}):
            process_note_memory(asdict(note))
        row=fm.get(SPACE,'extraction_context',note.id);row['verification_mode']=MODE
        fm.store.put(SPACE,'fm_extraction_context',note.id,row)
    def model(system, user, **kw):
        data=json.loads(user)
        if system==fact_agent.REVIEW:return {'approved':True,'reason':'模拟审核通过，正文保留两个来源事实。'}
        return {'tool':'propose_memory_changes','actions':[{'action':'create','memory_type':'semantic',
            'assertion_kind':'stated','content':'界面模拟验收：青石摄影社的相机存放在二楼蓝色柜子，柜子钥匙由林舟保管。',
            'source_fact_ids':[f['id'] for f in data['new_facts']], 'reason':'模拟方案：同一对象的补充信息。','entities':['青石摄影社']} ]}
    with patch.object(fact_agent,'complete_json',side_effect=model):
        run=fm.consolidate(SPACE,'semantic')
    row=fm.get(SPACE,'run',run['id']);row['verification_mode']=MODE;fm.store.put(SPACE,'fm_run',row['id'],row)
    trace=start_trace('memory_query',SPACE);trace['verification_mode']=MODE
    args={'query':'青石摄影社相机和柜子钥匙','memory_type':'semantic','limit':5,'min_score':0}
    add_step(trace,'query_routed',output_summary={'tool':'memory_search','safe_args':args},reason='界面模拟验收固定路线')
    rows=qa._run_tool(SPACE,'memory_search',args,trace=trace)
    with patch.object(qa,'_complete_json_with_hooks',return_value={'final_answer':'界面模拟验收：相机在二楼蓝色柜子，钥匙由林舟保管。'}):
        answer=qa._synthesize_answer('青石摄影社相机在哪里，谁保管钥匙？',[{'tool':'memory_search','args':args,'result':rows}],trace=trace)
    add_step(trace,'answer_returned',output_summary={'answer':answer});finish_trace(trace)
print(json.dumps({'space':SPACE,'mode':MODE,'run':run['id'],'query':trace['trace_id']},ensure_ascii=False))
