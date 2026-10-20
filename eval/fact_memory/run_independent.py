from pathlib import Path
import os,sys,json,time,hashlib,traceback,logging,threading
from datetime import datetime,timezone
from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor,as_completed
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
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
from storage.note_storage import save_note,find_note,NoteMetadata
from core.llm_client import complete_json,build_openai_client,extract_json_object
from core.config import get_chat_config
from independent_cases import OUT,CASES
from agent.query_agent import answer_question
from independent_grade import grade,add_answer_records
logging.getLogger().setLevel(logging.ERROR)
CHECKPOINTS=json.loads((OUT/'checkpoints.json').read_text())
RUN=datetime.now(timezone.utc).strftime('%H%M%S');lock=threading.Lock()

def dump(path,obj):path.write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=str))
def snap(s):return {k:fm.items(s,k) for k in ['fact','card','version','question','run','delete','tombstone','schedule']}
def note(s,n,t):
 d=NoteMetadata(id=n,message_id=n,space_id=s,ts=datetime.now(timezone.utc).isoformat(),title=t[:24],tags=[],type='记录',summary=t,text=t,related=[])
 save_note(d);assert find_note(s,n)['text']==t
 return asdict(d)
def f(s,t='用户住北京',n='n',kind='semantic',**kw):return fm.ingest_fact(s,note_id=s+'-'+n,original_text=t,content=t,memory_type=kind,enqueue=False,**kw)
def ca(row,**kw):return dict(action='create',content=row['content'],memory_type=row['memory_type'],assertion_kind='stated',source_fact_ids=[row['id']],reason='独立结构测试原文证据',**kw)
def run(s,actions,kind='semantic',**kw):return fm.consolidate(s,kind,planner=lambda *a:{'actions':actions},**kw)
def raises(fn,match=''):
 try: fn()
 except ValueError as e:
  assert match in str(e),(match,str(e));return str(e)
 raise AssertionError('expected ValueError')

def structural(case,s):
 op=case['operation'];e={}
 if op=='threshold':
  for i in range(29):f(s,f'故障事件{i}',str(i),'episodic')
  f(s);at=datetime(2026,10,20,17,tzinfo=timezone.utc)
  assert not fm.due(s,now=at);f(s,'故障事件28','28','episodic');assert len(fm.active_facts(s))==30
  f(s,'故障事件29','29','episodic');e['due']=fm.due(s,now=at);assert e['due']==[('episodic','threshold')]
 elif op=='daily':
  a=f(s);before=datetime(2026,10,19,18,59,tzinfo=timezone.utc);after=datetime(2026,10,19,19,tzinfo=timezone.utc)
  assert not fm.due(s,now=before);assert fm.due(s,now=after)==[('semantic','daily')]
  e['run']=run(s,[ca(a)],trigger='daily',now=after);f(s,'用户养猫','n2');assert not fm.due(s,now=datetime(2026,10,19,20,tzinfo=timezone.utc))
 elif op=='retry':
  a=f(s);e['error']=raises(lambda:run(s,[ca(a),{**ca(a),'source_fact_ids':['missing']}]))
  assert not fm.cards(s) and not fm.get(s,'fact',a['id'])['processed_at']
  assert fm.items(s,'run')[0]['status']=='failed';e['retry']=run(s,[ca(a)]);assert len(fm.cards(s))==1
 elif op=='lease':
  a=f(s);started=threading.Event();release=threading.Event()
  def planner(*x):started.set();release.wait(10);return {'actions':[ca(a)]}
  with ThreadPoolExecutor(max_workers=1) as pool:
   job=pool.submit(fm.consolidate,s,'semantic',planner=planner);assert started.wait(10)
   e['second']=fm.consolidate(s,'semantic',planner=planner);assert e['second']['status']=='busy';release.set();e['first']=job.result()
  assert len(fm.cards(s))==1
 elif op=='arrival':
  a=f(s)
  def planner(*x):e['new']=f(s,'用户养猫','n2');return {'actions':[ca(a)]}
  fm.consolidate(s,'semantic',planner=planner);assert fm.get(s,'fact',e['new']['id'])['processed_at'] is None
 elif op in ['review','reject']:
  a=f(s,'连续三周负责部署');run(s,[{**ca(a),'assertion_kind':'inferred'}]);c=fm.cards(s)[0]
  assert c['inferred_at'] and c['source_fact_ids']
  if op=='review':
   c=fm.review_memory(s,c['id'],decision='confirm',expected_version=c['version']);assert c['assertion_kind']=='inferred'
   c=fm.review_memory(s,c['id'],decision='edit',content='仅本月临时支援部署',expected_version=c['version']);assert c['assertion_kind']=='inferred' and c['user_locked'];assert len(fm.items(s,'version'))==3;e['review']=c
  else:
   fm.review_memory(s,c['id'],decision='reject',expected_version=c['version']);assert fm.get(s,'fact',a['id'])['status']=='active'
   with ws.transaction(s) as tx:
    a['processed_at']=None;tx.put('fm_fact',a['id'],a)
   e['error']=raises(lambda:run(s,[{**ca(a),'assertion_kind':'inferred'}]),'rejected');assert not fm.cards(s)
 elif op in ['delete_one','delete_all','resurrection','stale_delete']:
  t1='用户住北京';n1=s+'-n';note(s,n1,t1);a=f(s,t1);b=f(s,'用户的电脑购自北京','n2');note(s,s+'-n2',b['original_text']);run(s,[ca(a),ca(b)])
  e['before']=snap(s);r=fm.request_forget(s,'北京');assert len(fm.cards(s))==2
  if op=='delete_one':
   e['approval']=fm.approve_forget(s,r['id'],[a['id']]);assert fm.get(s,'fact',b['id'])['status']=='active';assert len(fm.cards(s))==1
  elif op=='delete_all':
   e['approval']=fm.approve_forget(s,r['id'],r['candidate_ids']);assert not fm.search_memory(s,'北京');assert find_note(s,n1)['text']==t1;assert find_note(s,s+'-n2')['text']==b['original_text'];assert e['approval']['status']=='completed'
  elif op=='resurrection':
   fm.approve_forget(s,r['id'],r['candidate_ids']);assert f(s,t1)['status']=='forgotten';e['rerun']=run(s,[]);assert not fm.cards(s)
  else:
   c=fm.cards(s)[0];fm.review_memory(s,c['id'],decision='edit',content='用户近期暂住北京',expected_version=c['version']);e['error']=raises(lambda:fm.approve_forget(s,r['id'],[c['id']]),'changed')
 elif op=='acl':
  a=f(s,'空间A独有兰花');f(s+'B','空间B独有茉莉');b=f(s,'alice私有海棠','n2',scope={'owner_id':'alice','access_scope':'owner'})
  assert not fm.search_memory(s+'B','兰花');assert not fm.search_memory(s,'海棠',access_context={'requester':'bob'});assert fm.search_memory(s,'海棠',access_context={'requester':'alice'});e['foreign_space']=snap(s+'B')
 elif op=='source_edit':
  a=f(s);run(s,[ca(a)]);f(s,'用户出生在北京');assert not fm.cards(s);assert fm.get(s,'fact',a['id'])['status']=='source_changed'
 elif op=='edit_race':
  a=f(s);run(s,[ca(a)]);c=fm.cards(s)[0];b=f(s,'用户搬到上海','n2')
  def planner(*x):
   fm.review_memory(s,c['id'],decision='edit',content='用户住北京，仅到上海出差',expected_version=c['version'])
   return {'actions':[{**ca(b),'action':'revise','target_ids':[c['id']]}]}
  e['error']=raises(lambda:fm.consolidate(s,'semantic',planner=planner),'version');assert '出差' in fm.cards(s)[0]['content'];assert not fm.get(s,'fact',b['id'])['processed_at']
 return e


def e2e(case,s,e):
 e.update({'messages':[],'snapshots':[]})
 for i,t in enumerate(case['messages']):
  n=note(s,f'{s}-m{i+1}',t);result=process_note_memory(n)
  e['messages'].append({'note':n,'result':result});e['snapshots'].append(snap(s))
  # Two rounds demonstrate before/after rather than only one bulk ingest.
  if i==0:
   
   try:e['initial_consolidation']=fm.manual_run(s)
   except Exception as exc:e['initial_consolidation_error']=f'{type(exc).__name__}: {exc}'
   e['after_first']=snap(s)
 e['before_final']=snap(s)
 try:e['final_consolidation']=fm.manual_run(s)
 except Exception as exc:e['final_consolidation_error']=f'{type(exc).__name__}: {exc}'
 e['after_final']=snap(s)
 if case.get('operation')=='answer':
  qs=[q for q in fm.items(s,'question') if q['status']=='open'];e['questions_before_answer']=qs
  if not qs: e['missing_expected_question']=True
  else:
   target_note=e['messages'][1]['note']['id']
   q=next((candidate for candidate in qs if any((fm.get(s,'fact',fid) or {}).get('note_id')==target_note for fid in candidate.get('source_fact_ids',[]))),qs[0])
   e['answered_question_id']=q['id']
   e['answer']=fm.answer_question(s,q['id'],'说的是海帆项目的生产环境，星河仍用甲方云。');e['after_answer']=fm.manual_run(s);e['answer_snapshot']=snap(s)
 e['query_hits']=memory_search(s,case['query'])
 e['query_hits_diagnostic_limit50']=memory_search(s,case['query'],limit=50)
 try:e['answer']=answer_question(s,case['query'],max_steps=4)
 except Exception as exc:e['answer_error']=f'{type(exc).__name__}: {exc}'
 e['legacy_memories']=[r.to_dict() for r in repository.list_memories(s)]
 # Inspect persisted Note again; preserving source is part of evidence chain.
 e['notes_preserved']=all(find_note(s,m['note']['id'])['text']==m['note']['text'] for m in e['messages'])
 assert e['notes_preserved']
 add_answer_records(s,e,snap(s))
 judge=grade(case,e,CHECKPOINTS[case['id']])
 e['judge']=judge
 return e,judge

def execute(case):
 s=f'fmeval_{RUN}_{case["id"].lower()}';start=time.time();r={'id':case['id'],'space':s,'level':case['level'],'category':case['category'],'started_at':datetime.now(timezone.utc).isoformat(),'status':'running','automatic':True,'human_reviewed':False}
 e={}
 try:
  if case['level'].startswith('真实'):
   e,j=e2e(case,s,e);r.update(status='pass' if j.get('passed') is True else 'fail',reason=j.get('reason'),issues=j.get('issues',[]),stages=j.get('stages',{}))
  else:e=structural(case,s);r.update(status='pass',reason='独立断言通过；仅证明结构与状态约束，不代表真实消息抽取语义通过。')
  r['evidence']=e
 except Exception as exc:
  if 'e' in locals():r['evidence']=e
  r.update(status='blocked' if ('Connection' in type(exc).__name__ or (isinstance(e,dict) and 'query_hits' in e and ('answer' in e or 'answer_error' in e))) else 'fail',reason=f'{type(exc).__name__}: {exc}',exception=traceback.format_exc())
 finally:
  r['final_state']=snap(s);r['seconds']=round(time.time()-start,2)
  dump(OUT/'traces'/f'{case["id"]}-{RUN}.json',r)
  print(json.dumps({k:r.get(k) for k in ['id','status','reason','seconds']},ensure_ascii=False),flush=True)
 return r

def main():
 OUT.mkdir(parents=True,exist_ok=True);(OUT/'traces').mkdir(exist_ok=True)
 casesfile=OUT/'cases.json';expectedhash=hashlib.sha256(casesfile.read_bytes()).hexdigest()
 meta={'run_id':RUN,'protocol_version':'round1-v1.2','checkpoints_sha256':hashlib.sha256((OUT/'checkpoints.json').read_bytes()).hexdigest(),'evaluator_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'grader_sha256':hashlib.sha256((Path(__file__).parent/'independent_grade.py').read_bytes()).hexdigest(),'dataset_sha256':expectedhash,'database':'suixinji_fact_memory_test_20261020','models':{r:get_chat_config(r).model for r in ['balanced','strong']},'extractor_mode':settings.MEMORY_EXTRACTOR_MODE,'feature_enabled':settings.FACT_MEMORY_ENABLED,'source_sha256':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in ['memory/fact_memory.py','memory/fact_agent.py','memory/fact_extraction.py','memory/service.py','memory/extractor.py','agent/query_agent.py']},'note':'自动模型判定并非人工复核；结构用例使用计划回调和直接事实注入；不宣称飞书/网页E2E。'}
 results=[]
 chosen=[c for c in CASES if not sys.argv[1:] or c['id'] in sys.argv[1:]]
 with ThreadPoolExecutor(max_workers=3) as pool:
  fs={pool.submit(execute,c):c for c in chosen}
  for future in as_completed(fs):
   results.append(future.result());dump(OUT/'results.json',{'metadata':meta,'results':sorted(results,key=lambda x:x['id'])})
 assert expectedhash==hashlib.sha256(casesfile.read_bytes()).hexdigest()
 dump(OUT/f'results-{RUN}.json',{'metadata':meta,'results':sorted(results,key=lambda x:x['id'])})
if __name__=='__main__':main()
