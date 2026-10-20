from pathlib import Path
import os,sys,json,time,hashlib,traceback,logging,threading
from datetime import datetime,timezone
from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor,as_completed
ROOT=Path(__file__).resolve().parents[3];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'eval/fact_memory'))
from dotenv import load_dotenv
load_dotenv(ROOT/'.env',override=True)
os.environ['SUIXINJI_FACT_MEMORY_ENABLED']='true'
os.environ['SUIXINJI_MEMORY_EXTRACTOR_MODE']='llm'
os.environ['SUIXINJI_STRONG_MODEL']='deepseek-v4-flash'
os.environ['SUIXINJI_BALANCED_MODEL']='deepseek-v4-flash'
os.environ['SUIXINJI_FAST_MODEL']='deepseek-v4-flash'
from sqlalchemy.engine import make_url
assert make_url(os.environ['DATABASE_URL']).database=='suixinji_fact_memory_test_20261020','wrong database'
from core import settings
assert settings.STORAGE_BACKEND=='postgres'
from infrastructure.database import get_engine
from sqlalchemy import text
with get_engine().connect() as c: assert c.scalar(text('select current_database()'))=='suixinji_fact_memory_test_20261020'
from memory import fact_memory as fm,workflow_state as ws,repository,fact_agent,fact_extraction
from memory.service import process_note_memory,memory_search
from storage.note_storage import save_note,find_note,NoteMetadata,update_note_metadata
from core.llm_client import complete_json,build_openai_client,extract_json_object
from core.config import get_chat_config,get_embedding_config
from cases import OUT,CASES
from audit_grade import grade,STAGES
from independent_grade import add_answer_records
from agent.query_agent import answer_question
from agent import query_agent as qa
# Observation only: preserve the exact post-mask payload returned to the query agent.
TOOL_OBSERVATIONS={};TOOL_LOCK=threading.Lock();ORIGINAL_RUN_TOOL=qa._run_tool
def observed_run_tool(space_id,action,args,**kwargs):
 result=ORIGINAL_RUN_TOOL(space_id,action,args,**kwargs)
 captured=json.loads(json.dumps(result,ensure_ascii=False,default=str))
 with TOOL_LOCK:TOOL_OBSERVATIONS.setdefault(space_id,[]).append({'tool':action,'args':dict(args),'returned_payload':captured})
 return result
qa._run_tool=observed_run_tool

def attach_actual_tool_payloads(space,e):
 observations=TOOL_OBSERVATIONS.get(space,[]);pool={}
 def collect(obj):
  if isinstance(obj,list):
   for item in obj:collect(item)
  elif isinstance(obj,dict):
   if obj.get('id'):pool[obj['id']]=obj
   for value in obj.values():
    if isinstance(value,(dict,list)):collect(value)
 for observation in observations:collect(observation['returned_payload'])
 e['actual_tool_observations']=observations
 e['legacy_reconstructed_answer_records']=e.get('actual_answer_records',[])
 e['actual_answer_records']=[pool[ident] for ident in e.get('actual_answer_record_ids',[]) if ident in pool]
 e['actual_answer_records_provenance']='exact post-mask tool return payloads; never raw-Note reconstruction'
 e['actual_answer_ids_without_captured_payload']=[ident for ident in e.get('actual_answer_record_ids',[]) if ident not in pool]

logging.getLogger().setLevel(logging.ERROR)
RUN=datetime.now(timezone.utc).strftime('%H%M%S')
CHECKPOINTS=json.loads((OUT/'checkpoints.json').read_text())
def dump(p,obj):p.write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=str))
def snap(s):return {k:fm.items(s,k) for k in ['fact','card','version','question','run','delete','tombstone','schedule','extraction_context']}
def note(s,n,t):
 d=NoteMetadata(id=n,message_id=n,space_id=s,ts=datetime.now(timezone.utc).isoformat(),title=t[:24],tags=[],type='记录',summary=t,text=t,related=[])
 save_note(d);assert find_note(s,n)['text']==t
 return asdict(d)
def e2e(case,s,e):
 e.update(messages=[],snapshots=[])
 for i,t in enumerate(case['messages']):
  n=note(s,f'{s}-m{i+1}',t);result=process_note_memory(n)
  e['messages'].append({'note':n,'result':result});e['snapshots'].append(snap(s))
  if i==0:
   e['first_immediate_hits']=memory_search(s,t)
   try:e['initial_consolidation']=fm.manual_run(s)
   except Exception as exc:e['initial_consolidation_error']=f'{type(exc).__name__}: {exc}'
   e['after_first']=snap(s)
   if case.get('operation') in ['edit','reject']:
    cards=fm.cards(s)
    assert cards,'framework cannot review nonexistent card'
    target=next((c for c in cards if any(fm.get(s,'fact',fid).get('note_id')==n['id'] for fid in c['source_fact_ids'])),None)
    assert target,'framework no card tied to first note'
    e['review_operation']=fm.review_memory(s,target['id'],decision=case['operation'],content=case.get('edit_content'),expected_version=target['version'])
 e['immediate_probe']={'prefix_messages':[m['note']['text'] for m in e['messages']],'query':case['query'],'hits':memory_search(s,case['query'])}
 e['before_final']=snap(s)
 try:e['final_consolidation']=fm.manual_run(s)
 except Exception as exc:e['final_consolidation_error']=f'{type(exc).__name__}: {exc}'
 e['after_final']=snap(s)
 if case.get('operation')=='clarification':
  qs=[q for q in fm.items(s,'question') if q['status']=='open'];e['questions_before_answer']=qs
  target_note=e['messages'][1]['note']['id']
  qs=[q for q in qs if any((fm.get(s,'fact',fid) or {}).get('note_id')==target_note for fid in q.get('source_fact_ids',[]))]
  if not qs:e['missing_expected_question']=True
  else:
   q=qs[0];e['answered_question_id']=q['id'];e['clarification']=fm.answer_question(s,q['id'],case['clarification']);e['after_clarification_run']=fm.manual_run(s);e['after_operation']=snap(s)
 if case.get('operation')=='forget':
  req=fm.request_forget(s,case['forget_query']);assert req['candidate_ids'],'framework no forget candidates'
  # Exact package identifier guards against partial-token matches selecting unrelated packages.
  ids=[c['id'] for c in req['candidates'] if case['forget_query'] in c['content']]
  assert ids,'framework no exact package forget candidates'
  e['forget_preview']=req;e['forget_approved_ids']=ids
  e['forget_operation']=fm.approve_forget(s,req['id'],ids);e['after_forget_run']=fm.manual_run(s);e['after_operation']=snap(s)
 if case.get('operation')=='source_edit':
  target=e['messages'][case['source_edit_index']]['note'];updated=update_note_metadata(s,target['id'],text=case['source_edit_text'],summary=case['source_edit_text'])
  assert updated and updated['text']==case['source_edit_text'],'framework source edit failed'
  e['source_edit_operation']={'before':target,'after':updated,'result':process_note_memory(updated,force=True)}
  e['after_source_edit_run']=fm.manual_run(s);e['after_operation']=snap(s)
 e['consolidated_hits']=memory_search(s,case['query'])
 e['query_hits']=memory_search(s,case['query'])
 e['query_hits_diagnostic_limit50']=memory_search(s,case['query'],limit=50)
 try:e['answer']=answer_question(s,case['query'],max_steps=4)
 except Exception as exc:e['answer_error']=f'{type(exc).__name__}: {exc}'
 e['legacy_memories']=[r.to_dict() for r in repository.list_memories(s)]
 e['notes_preserved']=all(find_note(s,m['note']['id'])['text']==(case['source_edit_text'] if case.get('operation')=='source_edit' and i==case['source_edit_index'] else m['note']['text']) for i,m in enumerate(e['messages']));assert e['notes_preserved']
 add_answer_records(s,e,snap(s));attach_actual_tool_payloads(s,e);j=grade(case,e,CHECKPOINTS[case['id']]);e['judge']=j
 return j

def execute(case):
 s=f'fmeval_r3_{RUN}_{case["id"].lower()}';start=time.time();e={}
 r={'id':case['id'],'space':s,'category':case['category'],'status':'running','automatic':True,'human_reviewed':False}
 try:
  j=e2e(case,s,e);r.update(status='pass' if j['passed'] else 'fail',reason=j.get('reason'),issues=j.get('issues',[]),stages=j.get('stages',{}))
 except Exception as exc:r.update(status='framework_error' if str(exc).startswith('framework') else 'error',reason=f'{type(exc).__name__}: {exc}',exception=traceback.format_exc())
 r['evidence']=e;r['final_state']=snap(s);r['seconds']=round(time.time()-start,2)
 dump(OUT/'traces'/f'{case["id"]}-{RUN}.json',r)
 print(json.dumps({k:r.get(k) for k in ['id','status','reason','seconds']},ensure_ascii=False),flush=True)
 return r

def main():
 OUT.mkdir(parents=True,exist_ok=True);(OUT/'traces').mkdir(exist_ok=True)
 freeze=json.loads((OUT/'dataset-freeze.json').read_text())
 assert freeze['case_count']==20 and freeze['message_count']>=80
 for n,h in freeze['sha256'].items():assert hashlib.sha256((OUT/n).read_bytes()).hexdigest()==h
 selected=[c for c in CASES if not sys.argv[1:] or c['id'] in sys.argv[1:]]
 source_paths=['memory/fact_memory.py','memory/fact_agent.py','memory/fact_extraction.py','memory/service.py','memory/extractor.py','memory/fact_retrieval.py','agent/query_agent.py','core/settings.py','core/llm_client.py','core/llm_key_pool.py']
 emb=get_embedding_config()
 meta={'embedding':{'model':emb.model,'dimension':emb.dimension,'endpoint_sha256':hashlib.sha256(emb.base_url.encode()).hexdigest()},'semantic_search_enabled':settings.FACT_MEMORY_SEMANTIC_SEARCH_ENABLED,'fact_memory_enabled':settings.FACT_MEMORY_ENABLED,'extractor_mode':settings.MEMORY_EXTRACTOR_MODE,'run_id':RUN,'protocol':'round3-retest-v1-per-stage-actual-records','database':'suixinji_fact_memory_test_20261020','case_count':len(selected),'message_count':sum(len(c['messages']) for c in selected),'dataset_sha256':freeze['sha256'],'models':{r:get_chat_config(r).model for r in ['fast','balanced','strong']},'source_sha256':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in source_paths},'evaluator_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'grader_sha256':hashlib.sha256((Path(__file__).parent/'audit_grade.py').read_bytes()).hexdigest(),'note':'真实Note流程与真实工具trace，自动模型评分不等于人工；没有结构注入混入分母。'}
 dump(OUT/f'metadata-{RUN}.json',meta);results=[]
 with ThreadPoolExecutor(max_workers=3) as pool:
  futures={pool.submit(execute,c):c for c in selected}
  for future in as_completed(futures):
   results.append(future.result());dump(OUT/f'results-{RUN}.json',{'metadata':meta,'results':sorted(results,key=lambda r:r['id'])})
 for n,h in freeze['sha256'].items():assert hashlib.sha256((OUT/n).read_bytes()).hexdigest()==h
 counts={k:{'pass':sum(r.get('stages',{}).get(k,{}).get('passed') is True for r in results),'total':len(results)} for k in STAGES}
 summary={'run_id':RUN,'pass':sum(r['status']=='pass' for r in results),'total':len(results),'failed':[r['id'] for r in results if r['status']!='pass'],'stage_counts':counts,'metadata':meta}
 dump(OUT/f'summary-{RUN}.json',summary);print(json.dumps(summary,ensure_ascii=False),flush=True)
if __name__=='__main__':main()
