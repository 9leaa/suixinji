'use strict';
// Context always follows the inspected run, never the message compose form.
function currentSpace(){return state.section==='consolidation'?state.consolidationSpace:state.run?.space_id||$('#space').value}
function businessNode(n){
 if(n.status==='error'||n.status==='failed'||n.status==='interrupted')return true;
 if(n.metadata_json?.snapshot)return true;
 if(n.kind==='memory'&&/关联检索|校验|合并决策|任务清单/.test(n.name))return false;
 return ['agent','planning','generation','classification','command','memory','delivery'].includes(n.kind)||
   n.name==='记忆检索最终结果'||n.kind==='tool';
}
$('#detailLevel').onchange=()=>{state.detailLevel=$('#detailLevel').value;renderView();resetCamera()};

function renderBusinessSummary(){
 const run=activeRun(),host=$('#businessSummary');if(!run){host.replaceChildren();return}
 const aggregate=run.spans.find(n=>n.name==='记忆处理'&&Array.isArray(n.output_json?.results));
 const effects=['ask','query'].includes(run.mode)?[]:aggregate?aggregate.output_json.results:run.spans.filter(n=>/写入|忘记|最终整理结果/.test(n.name)&&!n.name.includes('等待')&&n.output_json).map(n=>({...n.output_json,node_id:n.id}));
 const failures=run.spans.filter(n=>['error','failed','interrupted'].includes(n.status));
 const actionLabels={insert:'新增记忆',update:'更新记忆',fact_stored:'保存事实',skip:'跳过',skipped:'跳过',pending_review:'待确认',add_source:'补充来源',reject:'拒绝写入'};
 host.innerHTML=`<h2>本次业务结果</h2><p class="muted">空间 ${esc(run.space_id||currentSpace())} · ${esc(run.id)}${run.snapshot?' · 独立阶段快照':''}</p>`+
 `<p>${run.snapshot?'以下仅说明所选阶段，不代表整条消息的执行结果。':'按执行记录展示记忆处理结果；“完成”不等于抽取或回答一定正确。'}</p>`+
 (failures.length?`<p class="notice">${failures.length} 个失败或中断节点，展开详情查看原因。</p>`:'')+
 (effects.length?`<p>本次记录 ${effects.length} 项处理结果。</p>`+effects.map(o=>{const card=o.preference_card||{},content=o.content||o.description||(card.details||[]).map(d=>d.description).join('；');return `<article class="result-card"><strong>${esc(actionLabels[o.action]||o.action||'记忆处理结果')}</strong><p>${esc(content||'正文未保存在该执行结果中')}</p><small>${esc(o.memory_id||o.fact_id||'')} · ${esc(o.reason||'此结果未记录具体原因')}</small>${o.before||o.after?changeHTML({before:o.before,after:o.after,reason:o.reason}):''}<details><summary>当时的原文与完整结果</summary>${(card.details||[]).flatMap(d=>d.sources||[]).map(source=>`<blockquote>${esc(source.original_text||'原文未记录')}</blockquote>`).join('')}<pre>${esc(json(o))}</pre></details></article>`}).join(''):
 '<p class="muted">本次没有可确认的记忆写入记录。查询或阶段快照不代表发生记忆修改。</p>');
}
function readableVersion(v){
 if(!v)return '未记录，不能从当前数据库反推';
 if(v.withheld)return v.withheld;
 return typeof v==='string'?v:[v.content,v.status?'状态：'+v.status:'',v.task_status?'任务：'+v.task_status:''].filter(Boolean).join('\n')||'该版本没有正文记录';
}
function changeHTML(change){return `<div class="version-diff"><section><h4>修改前${change.before?.version?' · v'+esc(change.before.version):''}</h4><p>${esc(readableVersion(change.before))}</p></section><section><h4>修改后${change.after?.version?' · v'+esc(change.after.version):''}</h4><p>${esc(readableVersion(change.after))}</p></section></div><p class="muted">原因：${esc(change.reason||'未记录')}</p>`}

state.historyOffset=0;
async function history(){
 if(!state.token)return;
 const params=new URLSearchParams({space:$('#historySpace').value.trim(),q:$('#historyQuery').value.trim(),status:$('#historyStatus').value,offset:state.historyOffset,limit:30});
 for(const [key,id] of [['since','historySince'],['until','historyUntil']])if($('#'+id).value)params.set(key,new Date($('#'+id).value).toISOString());
 const request=state.historyRequest=(state.historyRequest||0)+1;
 const data=await api('/history?'+params);if(request!==state.historyRequest)return;
 $('#historyCount').textContent=`${data.total?data.offset+1:0}–${Math.min(data.offset+data.items.length,data.total)} / ${data.total}`;
 $('#historyPrev').disabled=data.offset===0;$('#historyNext').disabled=data.offset+data.limit>=data.total;
 $('#history').innerHTML=data.items.length?data.items.map(r=>`<button class="history-item ${state.run?.id===r.id?'active':''}" data-id="${esc(r.id)}"><small>${esc(r.space_id)} · ${esc(labels[r.status]||r.status)}</small><p>${esc(r.text)}</p><small>${esc(fullStamp(r.created_at))} · ${esc(r.mode)}</small></button>`).join(''):'<p class="empty-small">没有符合条件的运行。</p>';
 $$('.history-item').forEach(b=>b.onclick=()=>load(b.dataset.id).catch(e=>toast(e.message)));
}
$('#historyFilters').onsubmit=e=>{e.preventDefault();state.historyOffset=0;history().catch(e=>toast(e.message))};
$('#historyPrev').onclick=()=>{state.historyOffset=Math.max(0,state.historyOffset-30);history().catch(e=>toast(e.message))};
$('#historyNext').onclick=()=>{state.historyOffset+=30;history().catch(e=>toast(e.message))};

const typeLabels={preference:'偏好',task:'任务',semantic:'语义',episodic:'情景'};
$('#libraryBtn').onclick=()=>{if(!state.token){$('#settings').showModal();return}state.librarySpace=currentSpace();state.libraryOffset=0;$('#libraryPanel').showModal();loadLibrary()};
$('#closeLibrary').onclick=()=>$('#libraryPanel').close();
$('#libraryFilters').onsubmit=e=>{e.preventDefault();state.libraryOffset=0;loadLibrary()};
$('#libraryPrev').onclick=()=>{state.libraryOffset=Math.max(0,state.libraryOffset-30);loadLibrary()};
$('#libraryNext').onclick=()=>{state.libraryOffset+=30;loadLibrary()};
async function loadLibrary(){
 const request=state.libraryRequest=(state.libraryRequest||0)+1;
 $('#libraryScope').textContent='当前查看空间：'+state.librarySpace+' · 读取中…';$('#libraryRows').replaceChildren();
 try{
 const data=await api('/memories?'+new URLSearchParams({space:state.librarySpace,kind:$('#libraryKind').value,q:$('#libraryQuery').value,offset:state.libraryOffset||0}));
 if(request!==state.libraryRequest)return;
 $('#libraryScope').textContent='当前查看空间：'+data.space_id+' · '+data.scope;
 $('#libraryCounts').textContent=Object.entries(data.counts).map(([k,v])=>typeLabels[k]+' '+v+' 条').join(' · ');
 $('#libraryPage').textContent=`${data.total?data.offset+1:0}–${Math.min(data.offset+data.items.length,data.total)} / ${data.total}`;
 $('#libraryPrev').disabled=data.offset===0;$('#libraryNext').disabled=data.offset+data.limit>=data.total;
 $('#libraryRows').innerHTML=data.items.map((r,i)=>`<article class="result-card"><strong>${esc(typeLabels[r.memory_type]||'记忆')}</strong><p>${esc(r.withheld||r.content)}</p><small>${esc(r.id)} · ${esc(r.task_status||r.review_status||r.status||'')} · v${esc(r.version||r.current_version||'未记录')}</small>${r.withheld?'':`<button data-memory-index="${i}">原文与版本对比</button><div class="memory-history"></div>`}</article>`).join('')||'<p>当前筛选下没有有效记忆。</p>';
 $('#libraryRows').querySelectorAll('[data-memory-index]').forEach(b=>b.onclick=async()=>{
  const r=data.items[Number(b.dataset.memoryIndex)],target=b.nextElementSibling;b.disabled=true;
  try{const detail=await api('/memory-history?'+new URLSearchParams({space:data.space_id,ident:r.id,engine:r.engine}));
   target.innerHTML=`<h3>原文证据</h3><p class="muted">${esc(detail.source_notice)}</p>`+detail.sources.map(n=>`<blockquote>${esc(n.text)}<small>${esc(n.id)}</small></blockquote>`).join('')+
   '<h3>记录的版本变化</h3>'+ (detail.changes.map(changeHTML).join('')||'<p>没有历史版本快照。</p>');
  }catch(e){target.textContent=e.message}finally{b.disabled=false}
 });
 }catch(e){if(request===state.libraryRequest)$('#libraryScope').textContent='读取失败：'+e.message}
}
