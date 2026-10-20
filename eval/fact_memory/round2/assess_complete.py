"""Freeze transparent evidence adjudication of the SAME complete business run.
No model or business calls, and no changed case/gold/checkpoint files.
"""
import json,sys,copy,hashlib
from pathlib import Path
from cases import OUT,CASES
RUN='162147';data=json.loads((OUT/f'results-{RUN}.json').read_text());by={x['id']:x for x in CASES};rows=copy.deepcopy(data['results']);stages=['extraction','consolidation','immediate_retrieval','consolidated_retrieval','query_retrieval','answer']
for row in rows:
 e=row['evidence'];row['original_status']=row['status'];row['original_stages']=copy.deepcopy(row.get('stages',{}));changes=[]
 row['assessment']={'method':'assistant inspection of frozen gold, real tool payload, IDs and answer; not human review','gold_unchanged':True,'checked_expected':by[row['id']]['expected'],'checked_query':by[row['id']]['query'],'actual_record_ids_used':[x['id'] for x in e.get('actual_answer_records',[])],'changes':changes}
 def replace(stage,passed,reason,proof):
  previous=copy.deepcopy(row.get('stages',{}).get(stage));row.setdefault('stages',{})[stage]={'passed':passed,'reason':reason,'evidence':proof};changes.append({'stage':stage,'original':previous,'assessed':row['stages'][stage]})
 if row['id']=='R202':
  replace('answer',True,'答案明确给出9月二号、当前五号、CW-62没改，并实际提到团花在二号；原判称遗漏团花不实。stale引出的额外谨慎表述单列直接性边界，不抹去已正确回答的事实。',[
   {'record_id':'fm_38e241a46d570ac63b4180fd','channel':'actual_answer_records','quote':'澄湾馆的双鹤铜镜于2026年9月在二号展柜陈列。'},
   {'record_id':'fm_55a58d248181a04a2acdba53','channel':'actual_answer_records','quote':'2026年10月20日核对时，双鹤铜镜仍在五号展柜。'},
   {'record_id':'fm_8bcbbae8a6f0f11d5359c2e0','channel':'actual_answer_records','quote':'双鹤铜镜的馆藏号为CW-62，且该馆藏号一直未更改。'},
   {'answer_quote':'关于9月，记录显示“2026年9月在二号展柜陈列”'},
   {'answer_quote':'10月20日二号展柜陈列的是团花铜镜'}])
  row['assessment']['boundary']='答案把stale标记多说成历史需核实，直接性不足；不以单独stale标记作准确性失败。'
 if row['id']=='R203':
  replace('query_retrieval',True,'query只问LT-8当前/历史保存；默认8条与真实取证均覆盖4摄氏度冷藏、8月常温历史、LT-8铝箔袋。原判追加未问LT-9玻璃瓶的召回义务，超出固定问题范围。',[
   {'record_id':'fm_c39c907a77f1dfad3878db39','channel':'actual_answer_records','quote':'芦汀苗圃的LT-8种子批次自2026年10月1日起存放于4摄氏度冷藏柜；2026年8月存放于常温种子柜只是历史情况，当前仍为4摄氏度冷藏。'},
   {'record_id':'fm_38f26c7af38d0243ae8f88ef','channel':'actual_answer_records','quote':'LT-8的包装为铝箔袋。'}])
 if row['id']=='R211':
  fact_ids=[x['id'] for x in e['before_final']['fact']];probe_ids=[x['id'] for x in e['immediate_probe']['hits']]
  replace('extraction',True,'五条原Note抽取保留A12/B16、未指定主体的14小时变化与不得猜A/B限定；未错误合并主体。',[{'source_fact_ids':fact_ids}])
  replace('immediate_retrieval',True,'最终整理前的真实默认8条命中A/B原照明及未消解变化，满足当时前缀信息；用户补证尚未发生。',[{'actual_channel':'immediate_probe.hits','record_ids':probe_ids}])
  replace('consolidation',False,'正确补证后，同一目标同时add_evidence和revise触发preview自增版本，再报target version changed；四轮原样重交耗尽预算，补证整理未完成。',[{'run_id':'run_a6973cf36a014933bbae','duplicate_target_id':'fm_0c28ee65b4ce227dd5394a67','actual_steps':'final_state.run.trace'}])
  for stage in ['consolidated_retrieval','query_retrieval','answer']:
   replace(stage,None,'not_run：补证后整理失败，流程未进入最终检索/真实问答；不能用全库补做召回或给答案通过。',[])
 if row['id']=='R219':
  replace('answer',True,'原消息连续的10月15日→同日→当天明确支持雾岚8毫米属于15日。答案正确且如实说明上下文推定，不是无依据编造日期。派生卡日期自足性不足另在consolidation保留失败。',[
   {'record_id':'fm_c6e0da4145a12f472df70da0','channel':'actual_answer_records','quote':'雾岚站（露坂站附近）当天降雨量为8毫米。'},
   {'record_id':'fact_2b4fcfd1e80951c18aa339b7','channel':'actual_answer_records','quote':'露坂站2026年10月15日的降雨量是18毫米。'},
   {'source_note_ids':[m['note']['id'] for m in e['messages'][:3]],'original_order':['露坂站2026年10月15日的降雨量是18毫米。','同日露坂站观测到土壤含水率32%。','当天附近的雾岚站降雨量是8毫米。']},
   {'answer_quote':'雾岚站10月15日降雨量为8毫米。'}])
  row['assessment']['retained_real_failure']='fm_c6e0da4145a12f472df70da0仅写当天；唯一源fact_f7b41b43bac649cf8b7dc8fd也未补日期且无related_fact_ids。时间关联未独立保留到该卡，consolidation仍fail。'
 row['status']='pass' if all(row['stages'][s].get('passed') is True for s in stages) else 'fail'
 row['reason']='；'.join(row['stages'][s]['reason'] for s in stages if row['stages'][s].get('passed') is False) or ('完整固定预期通过；原评分差异及边界见assessment。' if changes else row['reason'])
# Validate all revised quote claims against the actual payload, never a database reread.
for row in rows:
 e=row['evidence'];actual={x['id']:x for x in e.get('actual_answer_records',[])}
 for change in row['assessment']['changes']:
  for proof in change['assessed']['evidence']:
   if proof.get('channel')=='actual_answer_records':
    record=actual[proof['record_id']];assert proof['quote'] in '\n'.join(str(record.get(k) or '') for k in ['content','text','original_text','evidence_span','title','summary'])
   if proof.get('answer_quote'):assert proof['answer_quote'] in e['answer']
meta={**data['metadata'],'assessor_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'assessment_protocol':'same-run-fixed-gold-evidence-v1','raw_results_path':str(OUT/f'results-{RUN}.json'),'raw_results_sha256':hashlib.sha256((OUT/f'results-{RUN}.json').read_bytes()).hexdigest(),'human_reviewed':False,'gold_modified':False,'business_rerun_for_assessment':False}
counts={s:{'pass':sum(x['stages'][s].get('passed') is True for x in rows),'fail':sum(x['stages'][s].get('passed') is False for x in rows),'not_run':sum(x['stages'][s].get('passed') is None for x in rows),'total':20} for s in stages}
summary={'run_id':RUN,'raw_pass':sum(x['original_status']=='pass' for x in rows),'assessed_pass':sum(x['status']=='pass' for x in rows),'total':20,'assessed_rate':sum(x['status']=='pass' for x in rows)/20,'failed_cases':[x['id'] for x in rows if x['status']!='pass'],'stage_counts':counts,'same_20_business_cases':True,'message_count':113,'human_review_count':0,'gold_unchanged':True,'metadata':meta}
(OUT/f'assessed-results-{RUN}.json').write_text(json.dumps({'metadata':meta,'summary':summary,'results':rows},ensure_ascii=False,indent=2));(OUT/f'assessed-summary-{RUN}.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2));print(json.dumps({k:v for k,v in summary.items() if k!='metadata'},ensure_ascii=False,indent=2))
