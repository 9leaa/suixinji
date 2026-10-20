"""Deterministic checks of saved evidence provenance; no DB or model calls."""
import sys,json,hashlib
from pathlib import Path
from cases import OUT,CASES
run=sys.argv[1];data=json.loads((OUT/f'results-{run}.json').read_text());rows=data['results'];meta=data['metadata']
assert {r['id'] for r in rows}=={c['id'] for c in CASES}
assert meta['database']=='suixinji_fact_memory_test_20261020'
assert all(v=='deepseek-v4-flash' for v in meta['models'].values())
assert meta['semantic_search_enabled'] is True
assert meta['embedding']['model']=='text-embedding-v4'
for name,h in meta['dataset_sha256'].items():assert hashlib.sha256((OUT/name).read_bytes()).hexdigest()==h
notes_check=json.loads((OUT/f'notes-preserved-{run}.json').read_text());assert notes_check['all_preserved'] and notes_check['total']==113
checks=[]
for row in rows:
 e=row['evidence']
 if 'actual_tool_observations' not in e:
  assert row['status']=='error'
  checks.append({'id':row['id'],'notes':len(e['messages']),'captured_tool_calls':0,'verified_support_quotes':0,'uncaptured_selected_ids':0,'pipeline_stopped_before_answer':True})
  continue
 assert e['notes_preserved'];assert not e['actual_answer_ids_without_captured_payload']
 assert len(e['query_hits'])<=8 and len(e['consolidated_hits'])<=8 and len(e['immediate_probe']['hits'])<=8
 assert e['primary_judge'].get('raw') and e.get('retrieval_judge_attempts')
 stages={'immediate_retrieval':{'hits':e['immediate_probe']['hits'],'first_probe':e['first_immediate_hits']},'consolidated_retrieval':{'hits':e['consolidated_hits']},'query_retrieval':{'hits':e['query_hits'],'actual_records':e['actual_answer_records']}}
 count=0
 for stage,channels in stages.items():
  for proof in row['stages'][stage]['evidence']:
   if proof.get('status')!='supported':continue
   records=channels.get(proof['channel'],[]);record=next((r for r in records if r['id']==proof['record_id']),None)
   assert record is not None
   content='\n'.join(str(record.get(k) or '') for k in ['content','text','summary','title','original_text','evidence_span'])
   assert proof['quote'] and proof['quote'] in content
   count+=1
 checks.append({'id':row['id'],'notes':len(e['messages']),'captured_tool_calls':len(e['actual_tool_observations']),'verified_support_quotes':count,'uncaptured_selected_ids':0})
forget=next(r for r in rows if r['id']=='R215')['evidence'];assert '蓝纹玛瑙' not in json.dumps(forget['actual_tool_observations'],ensure_ascii=False)
assert '蓝纹玛瑙' not in forget['answer'];assert '木制展架' in forget['answer']
assert all(f['status']=='forgotten' for f in forget['after_operation']['fact'] if f['id'] in forget['approval_scope']['target_fact_ids'])
assert any(f['status']=='active' for f in forget['after_operation']['fact'] if 'JL-509' in f['content'])
result={'run_id':run,'passed':True,'automatic':True,'human_reviewed':False,'case_count':len(rows),'real_message_count':sum(x['notes'] for x in checks),'captured_tool_calls':sum(x['captured_tool_calls'] for x in checks),'verified_support_quotes':sum(x['verified_support_quotes'] for x in checks),'uncaptured_selected_ids':0,'all_original_notes_preserved':True,'R215_forgotten_material_absent_from_actual_tool_returns_and_answer':True,'R215_unrelated_package_remains_active':True,'per_case':checks,'note':'只校验实际证据ID成员、连续原文片段与记录保护，不声称这些确定性检查能替代语义模型质量判定。'}
(OUT/f'evidence-verification-{run}.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps({k:v for k,v in result.items() if k!='per_case'},ensure_ascii=False,indent=2))
