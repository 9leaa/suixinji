import sys,json,hashlib,traceback
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,as_completed
sys.path.insert(0,str(Path(__file__).parent))
import run as runner
from audit_grade import grade
from cases import CASES,OUT
runid=sys.argv[1];selected=sys.argv[2:]
data=json.loads((OUT/f'results-{runid}.json').read_text());lookup={c['id']:c for c in CASES};rows=[]
def assess(original):
 row=json.loads(json.dumps(original));e=row['evidence'];row['original_judge']=e.get('judge');row['original_primary_judge']=e.get('primary_judge');row['original_retrieval_judge_attempts']=e.get('retrieval_judge_attempts');row['original_status']=row['status'];row['original_reason']=row.get('reason')
 try:
  j=grade(lookup[row['id']],e,runner.CHECKPOINTS[row['id']]);e['judge']=j;row.update(status='pass' if j['passed'] else 'fail',reason=j['reason'],stages=j['stages'],issues=j.get('issues',[]))
 except Exception as exc:row.update(status='framework_error',reason=str(exc),review_exception=traceback.format_exc())
 print(json.dumps({'id':row['id'],'status':row['status'],'reason':row['reason']},ensure_ascii=False),flush=True)
 return row
meta={**data['metadata'],'assessment_only':True,'original_results':str(OUT/f'results-{runid}.json'),'business_rerun':False,'audit_grader_sha256':hashlib.sha256((Path(__file__).parent/'audit_grade.py').read_bytes()).hexdigest()}
with ThreadPoolExecutor(max_workers=3) as pool:
 futures=[pool.submit(assess,r) for r in data['results'] if not selected or r['id'] in selected]
 for f in as_completed(futures):
  rows.append(f.result());runner.dump(OUT/f'assessed-results-{runid}.json',{'metadata':meta,'results':sorted(rows,key=lambda r:r['id'])})
