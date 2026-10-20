import json,hashlib
from pathlib import Path
from cases import OUT
RUN='161815'
d=json.loads((OUT/f'assessed-results-{RUN}.json').read_text());reviews=[]
def check_record(rows,ident,quote):
 r=next(x for x in rows if x['id']==ident);assert any(quote in str(r.get(k) or '') for k in ['content','text','summary','title','original_text','evidence_span']);return {'id':ident,'quote':quote}
def override(row,stage,passed,reason,proofs):
 reviews.append({'id':row['id'],'stage':stage,'original':row['stages'].get(stage),'reviewed':{'passed':passed,'reason':reason},'actual_record_proofs':proofs})
 row['stages'][stage]={'passed':passed,'reason':reason,'assistant_reviewed':True,'actual_record_proofs':proofs}
for r in d['results']:
 e=r['evidence']
 if r['id']=='R312':
  proofs=[check_record(e['after_final']['card'],'fm_2e163a8a1ff97b36e366cd25','每30天清洗'),check_record(e['after_final']['card'],'fm_56c4779e182f5e56f4998855','滤网保养周期为每60天清洗一次')]
  override(r,'consolidation',False,'编辑锁定卡为30天，但新综合卡复用其旧来源fact_7b33ba6d44e06576ad47ecfe又将60天作为当前值，真实派生冲突。',proofs)
  assert '每30天清洗一次' in e['answer'] and '2.6升' in e['answer'] and '240瓦' in e['answer']
  override(r,'answer',True,'回答正文明确以用户纠正的30天为准，2.6升240瓦正确；引用含旧值卡本身不构成虚假回答，整理缺陷另判。',[])
 if r['id']=='R314':
  rows=e['immediate_probe']['hits'];proofs=[]
  for ident,quote in [('fact_9ad5c3f385c7254088411482','蓝色收件回执'),('fact_d124d07640f895f7a2b7ae67','11月4日14点'),('fact_573d77807eea4e35cede9e0c','7号窗口')]:proofs.append(check_record(rows,ident,quote))
  override(r,'immediate_retrieval',True,'遗忘尚未发生。实际hits包含ZC551时间与携带物/窗口、ZC552时间与7号窗口，模型理由也明确判定通过但bool反写。',proofs)
 if r['id']=='R319':
  rows=e['query_hits'];assert not any('28日' in x['content'] or '10点' in x['content'] for x in rows)
  override(r,'query_retrieval',False,'query_hits默认8条缺具体待办/截止时间/未拿到报价；真实问答用Note兜底不能抵消该channel缺口。',[])
  assert '2026年10月28日上午10点' in e['answer'] and '还没有拿到报价' in e['answer']
  override(r,'answer',True,'真实回答具体待办、时间及未拿到报价均正确，使用实际返回Note符合既定规则；query未问营业时间，不能增添答题要求。',[])
 if r['id']=='R320':
  for stage,rows in [('immediate_retrieval',e['immediate_probe']['hits']),('query_retrieval',e['query_hits'])]:
   assert not any('不喜欢糖霜' in x['content'] or '仍选原味无糖霜' in x['content'] for x in rows)
   override(r,stage,False,'默认hits仅有原味不加葡萄干，漏不淋糖霜及试吃未改变偏好；实际问答快路也只返回exact司康偏好单卡。first_probe曾命中糖霜不能替代后续hits。',[])
 r['status']='pass' if all(v['passed'] for v in r['stages'].values()) else 'fail'
 r['reason']='；'.join(v['reason'] for v in r['stages'].values() if not v['passed']) or '六阶段通过；逐阶段实际返回记录审计并校验ID及原文片段。'
 r['human_reviewed']=False;r['assistant_reviewed']=r['id'] in ['R312','R314','R319','R320']
d['metadata']['assistant_review']='Explicit evidence-backed corrections preserve all primary/retrieval original judges in assessed file; no gold or business rerun.'
d['metadata']['reviewer_script_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
(OUT/'assistant-review.json').write_text(json.dumps({'human_reviewed':False,'reviews':reviews},ensure_ascii=False,indent=2))
(OUT/f'reviewed-results-{RUN}.json').write_text(json.dumps(d,ensure_ascii=False,indent=2))
print(json.dumps({'pass':sum(r['status']=='pass' for r in d['results']),'total':len(d['results']),'failed':[r['id'] for r in d['results'] if r['status']!='pass']},ensure_ascii=False))
