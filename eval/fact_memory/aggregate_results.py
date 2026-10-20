"""Merge latest attempt per fixed case; retain first and retry provenance."""
from pathlib import Path
import json,collections
from independent_cases import OUT,CASES
attempts=collections.defaultdict(list);meta={}
for p in sorted(OUT.glob('results-*.json')):
 try:
  v=json.loads(p.read_text());m=v.get('metadata',{});meta[m.get('run_id',p.stem)]=m
 except Exception:continue
for p in sorted((OUT/'traces').glob('FM*.json')):
 r=json.loads(p.read_text());r['artifact']=str(p);attempts[r['id']].append(r)
for p in sorted((OUT/'assessments').glob('FM*.json')):
 r=json.loads(p.read_text());r['artifact']=str(p);r['assessment_only']=True;attempts[r['id']].append(r)
rows=[]
for case in CASES:
 history=sorted(attempts[case['id']],key=lambda x:x['started_at'])
 if not history:rows.append({'id':case['id'],'status':'not_run','level':case['level'],'human_reviewed':False});continue
 r=history[-1];r['attempt_history']=[{k:h.get(k) for k in ['started_at','status','reason','artifact','space','assessment_only']} for h in history]
 run=r['space'].split('_')[1];r['source_metadata']=meta.get(run,{});rows.append(r)
real=[r for r in rows if r['id']<'FM19'];struct=[r for r in rows if r['id']>='FM19']
summary={'real_pass':sum(r['status']=='pass' for r in real),'real_total':18,'structural_pass':sum(r['status']=='pass' for r in struct),'structural_total':14,'human_review_count':0,'stage_counts':{k:{'pass':sum(r.get('stages',{}).get(k,{}).get('passed') is True for r in real),'total':18} for k in ['extraction','consolidation','retrieval','answer']},'failed':[r['id'] for r in real if r['status']!='pass']}
final_info=json.loads((OUT/'final-run.json').read_text()) if (OUT/'final-run.json').exists() else {}
if final_info:
 summary['stage_counts']=final_info['stage_counts']
 summary['frozen_full_run_id']=final_info['run_id']
out={'metadata':{'mode':'latest_attempt_per_fixed_case','note':'同一轮初测通过项保留，失败项使用修复后重测；每题保留来源版本与历史。评分修订不算新业务运行。','runs':meta,'summary':summary,'frozen_full_run':final_info},'results':rows}
(OUT/'results.json').write_text(json.dumps(out,ensure_ascii=False,indent=2,default=str));print(json.dumps(summary,ensure_ascii=False))
