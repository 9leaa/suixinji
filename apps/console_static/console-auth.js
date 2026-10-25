'use strict';
let registering=false;
const accountChannel=typeof BroadcastChannel==='function'?new BroadcastChannel('sxj-account'):null;
if(accountChannel)accountChannel.onmessage=()=>location.reload();
function isConnected(){return Boolean(state.account||state.token)}
function consoleHeaders(){return {'X-Suixinji-Request':'console',...(state.authMode==='token'?{Authorization:'Bearer '+state.token}:{})}}
function openAccountSettings(){
 if(state.authMode==='token'){$('#settings').showModal();return}
 if(state.account){$('#accountUsername').textContent='用户名：'+state.account.username;$('#accountDialog').showModal()}
 else $('#loginDialog').showModal();
}
async function authRequest(path,body){
 const response=await fetch('/api/console/auth/'+path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json','X-Suixinji-Request':'console'},body:body?JSON.stringify(body):undefined,cache:'no-store'});
 const data=await response.json();
 if(!response.ok)throw Error(typeof data.detail==='string'?data.detail:'请检查填写内容');
 return data;
}
async function connectAccount(account){
 state.account=account;
 document.body.dataset.accountRole=account.is_admin?'admin':'user';
 document.body.classList.add('account-mode');
 $('#space').value=account.space_id;$('#user').value=account.user_id;
 $('#space').readOnly=true;$('#user').readOnly=true;
 $('#settingsBtn').textContent='我的账号';
 $('#health').textContent='已登录 · '+account.username;
 $('#health').classList.add('online');
 const config=await api('/config');
 $('#factMemoryBtn').hidden=!account.is_admin||!config.fact_memory;
 $('#consolidationBtn').hidden=!account.is_admin||!config.fact_memory;
 if(!account.is_admin){
  $('.brand strong span').textContent='我的记忆';$('.brand small').textContent='YOUR PERSONAL MEMORY';
  $('#messagesBtn').textContent='我的消息';$('#runTitle').textContent='记下此刻，需要时再找回来';
  $('#runMode').textContent='我的随心记';$('#send').textContent='发送';
  $('#compose > .muted:last-child').textContent='记录、提问和回顾，都在你的私人空间中。';
  $('.sidebar > .section-title').textContent='写下想记住的事';
  $('#aliasUser').hidden=true;
 }
 await history();await loadCommands();
 document.body.classList.remove('auth-pending');
 if($('#loginDialog').open)$('#loginDialog').close();
 await ensureAlias();
}
$('#loginDialog').addEventListener('cancel',event=>event.preventDefault());
$('#switchAuth').onclick=()=>{
 registering=!registering;$('#loginTitle').textContent=registering?'创建你的随心记账号':'登录随心记';
 $('#loginSubmit').textContent=registering?'注册并开始使用':'登录';
 $('#switchAuth').textContent=registering?'已有账号，去登录':'注册新账号';
 $('#registerAliasLabel').hidden=!registering;$('#registerAlias').required=registering;
 $('#loginPassword').autocomplete=registering?'new-password':'current-password';$('#loginError').textContent='';
};
$('#loginForm').onsubmit=async event=>{
 event.preventDefault();$('#loginSubmit').disabled=true;$('#loginError').textContent='';
 try{
  const body={username:$('#loginUsername').value.trim(),password:$('#loginPassword').value};
  if(registering)body.alias=$('#registerAlias').value.trim();
  const account=await authRequest(registering?'register':'login',body);
  $('#loginPassword').value='';accountChannel?.postMessage('changed');await connectAccount(account);
 }catch(error){$('#loginError').textContent=error.message}finally{$('#loginSubmit').disabled=false}
};
$('#closeAccount').onclick=()=>$('#accountDialog').close();
$('#logout').onclick=async()=>{
 try{await authRequest('logout',{});accountChannel?.postMessage('changed');location.reload()}
 catch(error){toast(error.message)}
};
$('#passwordForm').onsubmit=async event=>{
 event.preventDefault();const button=$('#passwordForm button');button.disabled=true;
 try{await authRequest('password',{current_password:$('#currentPassword').value,new_password:$('#newPassword').value});accountChannel?.postMessage('changed');location.reload()}
 catch(error){$('#passwordError').textContent=error.message}finally{button.disabled=false}
};
document.addEventListener('visibilitychange',async()=>{
 if(document.hidden||!state.account)return;
 try{const account=await authRequest('me');if(account.user_id!==state.account.user_id)location.reload()}
 catch{location.reload()}
});
async function bootstrapAccount(){
 try{
  const options=await authRequest('options');state.authMode=options.mode;
  if(options.mode==='token'){document.body.classList.remove('auth-pending');$('#settings').showModal();return}
  $('#switchAuth').hidden=!options.registration;
  const response=await fetch('/api/console/auth/me',{cache:'no-store'});
  if(response.status===401){$('#loginDialog').showModal();return}
  if(!response.ok)throw Error('登录服务暂不可用，请稍后刷新');
  await connectAccount(await response.json());
 }catch(error){$('#loginDialog').showModal();$('#loginError').textContent=error.message}
}
bootstrapAccount();
