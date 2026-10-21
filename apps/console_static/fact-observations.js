// All evidence below is captured at execution time; no current-row reconstruction.
let observationsData={}, observationStage='extraction';
const stageTitles={extraction:'① 即时处理',consolidation:'② 记忆整理',query:'③ 查询回答'};
const toolTitles={search_memory:'检索相关记忆',inspect_memory:'查看来源证据',propose_memory_changes:'提出整理方案',validate_proposal:'执行前校验',review_proposal:'独立证据审核',model_context:'模型实际上下文',query_received:'接收问题',query_plan:'查询计划',query_routed:'选择查询路径',memory_search:'检索记忆',note_search:'查询笔记 / 补查',evidence_selected:'选择证据',rerank:'排序结果',answer_generated:'生成回答',answer_returned:'返回答案',trace_finished:'结束',memory_watermark_barrier:'等待记忆写入'};
function section(title){const e=node('section',undefined,'ob-step');e.append(node('h3',title));return e}
function warning(parent,text){parent.append(node('p',text,'ob-warning'))}
function records(parent,title,rows,ids){
 const d=node('details',undefined,'ob-records');d.append(node('summary',title+' · '+(Array.isArray(rows)?rows.length+' 条':Array.isArray(ids)?ids.length+' 个已记录 ID':'数量未记录')));parent.append(d);
 if(!Array.isArray(rows)){warning(d,'旧记录未保存当时正文，不能以当前记忆替代。');if(ids?.length)d.append(node('pre',ids.join('\n')));return}
 if(!rows.length){d.append(node('p','本次没有结果。','muted'));return}
 for(const [i,r] of rows.entries()){
  const e=node('article',undefined,'ob-evidence');e.append(node('strong',`${i+1}. ${r.content||r.text||r.title||r.id||'记录'}`));
  e.append(node('p',[r.id,r.memory_type,r.status].filter(Boolean).join(' · '),'muted'));
  const scores=['lexical_score','semantic_score','score'].filter(k=>r[k]!=null).map(k=>({lexical_score:'词面',semantic_score:'语义',score:'融合'}[k])+': '+Number(r[k]).toFixed(4));
  if(scores.length)e.append(node('p',scores.join(' / '),'ob-score'));
  if(r.evidence_span)e.append(node('p','引用原话：'+r.evidence_span,'content'));
  for(const src of r.sources||[])if(src.original_text)e.append(node('p','来源原话：'+src.original_text,'content'));
  e.append(details('记录全部字段',r));d.append(e)
 }
}
function renderActions(parent,actions){
 const names={create:'新建',add_evidence:'追加证据',revise:'更新',merge:'合并',split:'拆分',link:'关联',retract:'撤回',resolve_reference:'消解指代',defer:'向用户提问'};
 for(const a of actions||[]){const e=node('article',undefined,'ob-evidence');e.append(node('strong',(names[a.action]||a.action)+' · '+a.action),node('p',a.content||a.question||a.resolution||a.relation||'','content'),node('p','依据：'+(a.reason||'未记录'),'muted'),details('来源、目标和全部参数',a));parent.append(e)}
}
function contextEvidence(parent,context){
 if(context.evidence)records(parent,'审核模型读到的证据',context.evidence);
 if(context.related)records(parent,'实际传给整理模型的相关证据',context.related);
 if(context.new_facts)records(parent,'本批新事实',context.new_facts);
 if(context.observations){for(const o of context.observations){const d=node('details');d.append(node('summary','模型读到的 '+o.tool+(o.thought?' · '+o.thought:'')));records(d,'工具证据',Array.isArray(o.result)?o.result:undefined);if(!Array.isArray(o.result))d.append(details('结构化工具结果',o.result));parent.append(d)}}
 parent.append(details('完整模型输入（执行时快照）',context));
}
function extractionView(r,root){
 let e=section('1 · 输入消息与查询条件');e.append(node('p',r.query||'未记录消息正文','content'),details('查询参数',r.parameters||'旧记录未记录参数'));root.append(e);
 e=section('2 · 召回的候选');records(e,'相关记忆召回',r.retrieved);records(e,'最近事实补充',r.recent);root.append(e);
 e=section(r.status==='completed'?'3 · 抽取成功时的模型上下文':'3 · 准备的抽取上下文');if(r.status!=='completed')warning(e,'本次上下文已准备，但尚无抽取成功记录；不能据此认定模型已成功读取。状态：'+(r.status||'旧记录未记录'));e.append(node('p',r.selection||'旧记录仅保存上下文 ID','muted'));records(e,r.status==='completed'?'实际上下文':'准备的上下文',r.model_context,r.result_ids);root.append(e);
}
function consolidationView(r,root){
 const intro=section('1 · 整理任务');intro.append(node('p',`触发：${r.trigger||'未记录'} · 类型：${r.memory_type||'未记录'} · 状态：${r.status||'未记录'}`),details('本批事实 ID',r.fact_ids||[]));root.append(intro);
 if(!(r.trace||[]).some(t=>t.tool==='model_context'))warning(root,!(r.fact_ids||[]).length&&!(r.trace||[]).length?'本次没有待整理事实，未调用整理模型。':'本次未保存逐轮模型输入快照；以下仅显示实际留存的工具记录。');
 for(const [i,t] of (r.trace||[]).entries()){
  const tool=t.tool||t.response?.tool||(t.response?.actions?'propose_memory_changes':'model_response');
  const e=section(`${i+2} · ${toolTitles[tool]||tool}`);e.append(node('small',tool+(t.phase==='initial_retrieval'?' · 系统预召回':t.phase==='review'?' · 审核阶段':'')+(t.round!=null?' · 第 '+(t.round+1)+' 轮':'')));
  if(tool==='model_context')contextEvidence(e,t.context);
  else if(tool==='search_memory'||tool==='inspect_memory'){if(t.args)e.append(details('调用参数',t.args));records(e,'工具返回',t.result,t.result_ids)}
  else if(t.response){renderActions(e,t.response.actions);e.append(details('模型选择的工具 / 参数 / 理由',t.response))}
  else {e.append(details('校验或审核结果',t));if(t.errors||t.result?.approved===false)warning(e,'此步未通过，请展开具体原因。')}
  root.append(e);
 }
 const end=section('最终提交结果');end.append(node('p',r.status==='completed'?'本次整理已提交。':'本次未确认完成提交。'));if(r.error)warning(end,r.error);end.append(details('最终方案',r.plan||'未记录提交方案'));root.append(end)
}
function queryView(r,root){
 const steps=r.steps||[], route=steps.find(s=>s.step==='query_routed'), args=route?.output_summary?.safe_args||{}, calls=steps.filter(s=>['memory_search','note_search'].includes(s.step));
 const noteTools=new Set(['search_notes','get_related_notes','by_type','by_tag','filter_notes','semantic_search','get_note','list_recent','follow_links','related_notes','memory_note_fallback']);
 const intro=section('1 · 查询范围与补查');const question=steps.find(s=>s.step==='model_context')?.input_summary?.question;if(question)intro.append(node('p',question,'content'));intro.append(node('p',`记忆类型限制：${args.memory_type||'未限定或旧记录未记录'} · 工具调用 ${calls.length} 次 · 笔记查询 ${calls.filter(s=>noteTools.has(s.input_summary?.tool)||s.step==='note_search'&&!s.input_summary?.tool).length} 次`));
 if(args.memory_type)warning(intro,'本次路由限定 '+args.memory_type+'。其他类型是否被查到，请核对下面每次调用，不能以库中存在代替已召回。');
 if(!steps.some(s=>s.step==='model_context'))warning(intro,'旧记录未保存模型输入快照。选中证据 ID 不等于完整模型输入。');root.append(intro);
 for(const [i,s] of steps.entries()){
  const e=section(`${i+2} · ${toolTitles[s.step]||s.step}`);e.append(node('small',`${s.step} · ${s.status||''} · ${localTime(s.at)}${s.duration_ms>0?' · '+s.duration_ms+' ms':' · 未单独计时'}`));
  if(s.reason)e.append(node('p',s.reason,'muted'));if(s.error)warning(e,s.error);
  if(s.step==='model_context')contextEvidence(e,s.input_summary||{});
  else if(['memory_search','note_search'].includes(s.step)){e.append(details('实际调用参数',s.input_summary));records(e,'实际工具返回',s.output_summary?.records,s.output_summary?.ids)}
  else if(s.step==='answer_returned'&&s.output_summary?.answer)e.append(node('p',s.output_summary.answer,'content'));
  else e.append(details('输入与输出', {input:s.input_summary,output:s.output_summary}));
  root.append(e)
 }
}
function renderObservationList(){
 const select=$('ob-run'), previous=select.value;select.replaceChildren();const term=$('ob-search').value.toLowerCase();
 const rows=(observationsData[observationStage]||[]).filter(r=>JSON.stringify(r).toLowerCase().includes(term));
 for(const r of rows){const id=r.id||r.trace_id;const o=node('option',localTime(r.created_at||r.started_at)+' · '+(r.query||r.memory_type||r.status||'')+' · '+id);o.value=id;select.append(o)}
 if(rows.some(r=>(r.id||r.trace_id)===previous))select.value=previous;
 $('ob-count').textContent=`空间 ${observationsData.space_id||''} · ${rows.length} 条运行（每阶段最近 ${observationsData.limit||20} 条）`;
 renderObservation();
}
function renderObservation(){
 const root=$('ob-flow');root.replaceChildren();const r=(observationsData[observationStage]||[]).find(r=>(r.id||r.trace_id)===$('ob-run').value);
 if(!r){root.append(node('p','当前空间没有符合条件的记录。发送消息、整理或提问后，点击刷新。','muted'));return}
 if(r.withheld){warning(root,r.withheld);return}
 if(r.verification_mode)warning(root,r.verification_mode);
 ({extraction:extractionView,consolidation:consolidationView,query:queryView}[observationStage])(r,root);
 root.append(details('本次原始诊断记录',r));
}
async function refreshObservations(){
 const space=$('space').value;const r=await fetch('/api/fact-memory/'+encodeURIComponent(space)+'/observations',{headers:{Authorization:'Bearer '+$('token').value}});
 const value=await r.json();if(!r.ok)throw Error(value.detail||r.status);if(space!==$('space').value)return;
 observationsData=value;
 for(const b of document.querySelectorAll('[data-ob-stage]'))b.textContent=stageTitles[b.dataset.obStage]+' · '+(value[b.dataset.obStage]||[]).length;
 renderObservationList();
}
for(const b of document.querySelectorAll('[data-ob-stage]'))b.onclick=()=>{observationStage=b.dataset.obStage;document.querySelectorAll('[data-ob-stage]').forEach(x=>{x.classList.toggle('primary',x===b);x.setAttribute('aria-pressed',String(x===b))});renderObservationList()};
$('ob-refresh').onclick=()=>action(refreshObservations);$('ob-search').oninput=renderObservationList;$('ob-run').onchange=renderObservation;
