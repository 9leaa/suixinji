"""Freeze one complete, same-source run after independent evidence review."""
import sys,json,hashlib
from pathlib import Path
from independent_cases import OUT,CASES
ROOT=Path(__file__).resolve().parents[2];run=sys.argv[1]
r=json.loads((OUT/f'results-{run}.json').read_text());rows=r['results'];meta=r['metadata']
rows=[json.loads((OUT/'assessments'/f'{x["id"]}-{run}.json').read_text()) if (OUT/'assessments'/f'{x["id"]}-{run}.json').exists() else x for x in rows]
assert {x['id'] for x in rows}=={c['id'] for c in CASES},'not a complete 32-case run'
assert hashlib.sha256((OUT/'cases.json').read_bytes()).hexdigest()==meta['dataset_sha256'],'dataset drift'
assert hashlib.sha256((OUT/'checkpoints.json').read_bytes()).hexdigest()==meta['checkpoints_sha256'],'checkpoint drift'
disk_changes={p:{'runtime_start_sha256':h,'current_disk_sha256':hashlib.sha256((ROOT/p).read_bytes()).hexdigest()} for p,h in meta['source_sha256'].items() if hashlib.sha256((ROOT/p).read_bytes()).hexdigest()!=h}
assert hashlib.sha256((ROOT/'eval/fact_memory/run_independent.py').read_bytes()).hexdigest()==meta['evaluator_sha256'],'evaluator changed during run'
assert hashlib.sha256((ROOT/'eval/fact_memory/independent_grade.py').read_bytes()).hexdigest()==meta['grader_sha256'],'grader changed during run'
real=[x for x in rows if x['id']<'FM19'];struct=[x for x in rows if x['id']>='FM19']
assert len(real)==18 and all(x['status'] in ['pass','fail'] for x in real),'some real cases unscored'
assert all(x['status']=='pass' for x in struct),'structural failure'
p=sum(x['status']=='pass' for x in real);assert p/18>=.85,'below acceptance target'
acl=json.loads((OUT/'auxiliary-acl-audit-after.json').read_text());assert acl['passed'],'ACL audit failed'
assert acl['source_sha256']==meta['source_sha256']['memory/fact_memory.py'],'ACL audit not current source'
out={'run_id':run,'same_source_complete_run':True,'real_pass':p,'real_total':18,'real_rate':p/18,'structural_pass':14,'structural_total':14,'human_review_count':0,'stage_counts':{k:{'pass':sum(x.get('stages',{}).get(k,{}).get('passed') is True for x in real),'fail':sum(x.get('stages',{}).get(k,{}).get('passed') is False for x in real),'not_run':sum(not isinstance(x.get('stages',{}).get(k,{}).get('passed'),bool) for x in real),'total':18} for k in ['extraction','consolidation','retrieval','answer']},'failed_cases':[x['id'] for x in real if x['status']!='pass'],'source_sha256':meta['source_sha256'],'dataset_sha256':meta['dataset_sha256'],'checkpoints_sha256':meta['checkpoints_sha256'],'models':meta['models'],'acl_audit_passed':True,'actual_real_message_count':55,'structural_operation_count':42,'disk_changes_after_run_started':disk_changes,'runtime_import_policy':'所有被测模块在启动并行测试前预加载，测试不reload；后续磁盘补丁与独立附测不拼入原完整轮分数。','known_residual':'FM12缺少疑问，补证后的回答检查点未跑。','note':'所有18组为同一源码/评测器/评分器进程，全新space；评分与结构均非人工复核。UI/飞书端到端另由主代理验证。'}
(OUT/'final-run.json').write_text(json.dumps(out,ensure_ascii=False,indent=2));print(json.dumps(out,ensure_ascii=False,indent=2))
