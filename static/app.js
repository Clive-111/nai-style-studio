'use strict';

const paths = {
  layers:'<path d="m12 3 9 5-9 5-9-5zM3 12l9 5 9-5M3 16l9 5 9-5"/>',
  image:'<rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="8" cy="9" r="1.3"/><path d="m4 17 5-5 4 4 3-3 4 4"/>',
  'image-plus':'<path d="M19 12v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h8M3 16l5-5 4 4 2-2 5 5M19 2v7m-3.5-3.5h7"/><circle cx="8" cy="8" r="1"/>',
  upload:'<path d="M12 16V3m-4.5 4.5L12 3l4.5 4.5M4 15v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4"/>',
  sliders:'<path d="M4 7h7m5 0h4M4 17h3m5 0h8"/><circle cx="13" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
  copy:'<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M15 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/>',
  bookmark:'<path d="M6 4h12a1 1 0 0 1 1 1v16l-7-4.5L5 21V5a1 1 0 0 1 1-1z"/>',
  search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  code:'<path d="m8 7-5 5 5 5m8-10 5 5-5 5m-3-13-2 16"/>',chevron:'<path d="m6 9 6 6 6-6"/>',
  file:'<path d="M14 3H5v18h14V8zm0 0v5h5"/>', x:'<path d="m6 6 12 12M6 18 18 6"/>'
};
function icon(name) {
  const el = document.createElementNS('http://www.w3.org/2000/svg','svg');
  for (const [key,val] of Object.entries({viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':'1.65','stroke-linecap':'round','stroke-linejoin':'round',class:'ico','aria-hidden':'true'})) el.setAttribute(key,val);
  el.innerHTML=paths[name] || '';
  return el;
}
document.querySelectorAll('[data-icon]').forEach(el=>el.append(icon(el.dataset.icon)));
const $=id=>document.getElementById(id);
const el=(tag,cls,value)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(value!=null)n.textContent=value;return n;};
const state={styles:[], detail:null, example:null, draft:null, original:{}, busy:false, pendingTarget:null, loadId:0};
let toastTimer;
function toast(message,error=false){
  $('toast').textContent=message;
  $('toast').className=error?'toast show error':'toast show';
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').className='toast',error?5500:2700);
}
async function api(url,options={}){
  const response=await fetch(url,{cache:'no-store',...options});
  const data=await response.json();
  if(!response.ok)throw new Error(data.error || '本机服务请求失败');
  return data;
}
const jsonOptions=(method,payload)=>({method,headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
function report(err){toast(err.message||'操作失败，请重试',true);}
function busy(value){state.busy=value;$('open').disabled=value;$('replace').disabled=value;$('confirm').disabled=value;$('choose').disabled=value;$('set-cover').disabled=value;syncForm();}
const toLF=text=>String(text??'').replace(/\r\n?/g,'\n');
function valueOf(field){
  const source=state.original[field]??'';
  return $(field).value===toLF(source)?source:$(field).value;
}
const fields=()=>({name:valueOf('name'),description:valueOf('description'),recipe:valueOf('recipe')});
const changed=()=>Object.keys(state.original).some(key=>$(key).value!==toLF(state.original[key]));
function canLeave(){
  return (!state.draft && !changed()) || window.confirm('当前有待保存的画风。要放弃这次编辑并继续吗？');
}
let exampleNegative='', currentMetadata={};
function syncForm(){
  const hasRecipe=Boolean($('recipe').value.trim());
  $('char-count').textContent=$('recipe').value.length.toLocaleString()+' 字符';
  $('copy').disabled=state.busy||!hasRecipe;
  $('copy-negative').disabled=state.busy||!exampleNegative;
  $('save').disabled=state.busy||!hasRecipe||(!state.detail&&!state.draft)||(!state.draft&&!changed());
  $('save').querySelector('span').textContent=state.detail&&!state.draft?'保存修改':'保存到收藏';
  for(const id of ['name','description','recipe'])$(id).disabled=state.busy;
}
function putForm(item,mode,warnings=[]){
  state.original={name:item.name||'',description:item.description||'',recipe:item.recipe||''};
  for(const [key,text] of Object.entries(state.original))$(key).value=text;
  $('mode').textContent=mode;
  $('warnings').textContent=warnings.join('\n');$('warnings').hidden=!warnings.length;
  $('raw').open=false;syncForm();
}
for(const id of ['name','description','recipe'])$(id).addEventListener('input',syncForm);
const recipeEditor=RecipeRepair.bind($('recipe'),{read:()=>valueOf('recipe'),onChange:syncForm,notify:toast});

const labels={prompt:'正向提示词',negative:'负面提示词',character_prompt:'角色正向提示词',character_negative:'角色负面提示词',model:'模型',width:'宽度',height:'高度',seed:'种子',steps:'步数',sampler:'采样器',schedule:'噪声调度',guidance:'提示词引导值',rescale:'引导重缩放',uc_strength:'负面提示词强度',raw:'原始元数据'};
function metadata(meta={}){
  currentMetadata=meta;
  exampleNegative=typeof meta.negative==='string'?meta.negative:'';
  $('negative').value=exampleNegative;
  $('copy-negative').disabled=state.busy||!exampleNegative;
  const samplers={k_dpmpp_2m_sde:'DPM++ 2M SDE',k_dpmpp_2m:'DPM++ 2M',k_euler:'Euler',k_euler_ancestral:'Euler Ancestral'};
  const params=[
    ['Sampler',samplers[meta.sampler]??meta.sampler],['Steps',meta.steps],
    ['Prompt Guidance',meta.guidance],['Prompt Guidance Rescale',meta.rescale],
    ['负面提示词强度',meta.uc_strength],['噪声调度',meta.schedule==='karras'?'Karras':meta.schedule]
  ];
  $('quick-params').replaceChildren(...params.map(([label,value])=>{
    const row=el('div');row.append(el('dt','',label),el('dd','',value==null||value===''?'未记录':String(value)));return row;
  }));
  const box=$('metadata');box.replaceChildren();
  const entries=[...new Set([...Object.keys(labels),...Object.keys(meta)])];
  for(const key of entries){
    const v=meta[key];if(v==null||v===''||(Array.isArray(v)&&!v.length))continue;
    const div=el('div','meta-item');
    div.append(el('b','',labels[key]||key),el('pre','',typeof v==='object'?JSON.stringify(v,null,2):String(v)));box.append(div);
  }
  if(!box.children.length)box.textContent='这张图片没有可显示的生成参数。';
}
function preview(src,title,meta,caption){
  const image=$('preview');image.hidden=!src;$('empty-art').hidden=Boolean(src);
  if(src){image.alt=title;image.src=src;}else{image.removeAttribute('src');image.alt='';}
  $('file-label').textContent=caption||'等待原图';$('file-label').title=caption||'';
  $('empty-title').textContent=state.detail?'这套画风还缺一张例图':'拖入一张 NovelAI 原图';
  $('choose').textContent=state.detail?'添加例图':'选择原图';
  $('dimensions').textContent=meta.width&&meta.height?meta.width+' × '+meta.height:'';
  metadata(meta);
}
function clearDraft(){
  if(state.draft)URL.revokeObjectURL(state.draft.url);
  state.draft=null;
}
function setExample(item){
  state.example=item??null;const info=state.detail;
  const archived=item?.image_kind==='archive', shot=item?.image_kind==='preview';
  const caption=archived?'TXT 存档 · 待添加原图':shot?'已归档的画面预览':item?.filename||info.name;
  preview(item?.image_url,info.name,item?.metadata||{},caption);
  const list=$('example-list');list.replaceChildren();
  $('example-tools').hidden=false;
  for(const [i,ex] of info.examples.entries()){
    const btn=el('button','example-thumb'+(ex.id===item?.id?' selected':''));
    btn.title=ex.image_url?'查看例图 '+(i+1):'查看已归档的提示词与参数';
    btn.setAttribute('aria-label',btn.title);btn.setAttribute('aria-pressed',String(ex.id===item?.id));
    if(ex.image_url){const img=el('img');img.src=ex.image_url;img.alt='';btn.append(img);}else btn.textContent='TXT';
    btn.onclick=()=>{if(!state.busy)setExample(ex);};list.append(btn);
  }
  const add=el('button','example-thumb example-add','+');add.title='添加例图';add.setAttribute('aria-label','添加例图');add.onclick=()=>pick(info.id);list.append(add);
  $('set-cover').hidden=!item?.image_url||item.id===info.cover_id;
}
async function detail(id,preferredExample=null){
  const token=++state.loadId;
  const info=await api('/api/styles/'+id);if(token!==state.loadId)return;
  clearDraft();state.detail=info;
  putForm(info,'已收藏');
  setExample(info.examples.find(e=>e.id===preferredExample)||info.examples.find(e=>e.id===info.cover_id)||info.examples[0]);
  renderCards();
}
async function styles(){state.styles=(await api('/api/styles')).styles;renderCards();}
function normalize(value){return value.trim().replace(/\s*[\r\n]+\s*/g,' ').replace(/,\s*/g,',');}
function modelBadge(model){
  const match=/^(?:(?:novelai|nai)[\s_-]+(?:diffusion[\s_-]+)?(?:anime[\s_-]+|furry[\s_-]+)?v?|v)(\d+)(?:[._-](\d+))?(?=$|[\s_-])/i.exec(model);
  const badge=el('span','model-badge',match?'V'+match[1]+(match[2]?'.'+match[2]:''):'未知');
  badge.title=model||'封面未记录模型';
  badge.setAttribute('aria-label','封面模型：'+badge.title);
  return badge;
}
function renderCards(){
  const query=$('search').value.trim().toLocaleLowerCase();
  const cards=$('cards');cards.replaceChildren();
  $('library-count').textContent=state.styles.length;
  for(const [index,item] of state.styles.entries()){
    if(![item.name,item.description,item.recipe].some(v=>(v||'').toLocaleLowerCase().includes(query)))continue;
    const card=el('article','card'+(item.id===state.detail?.id?' selected':''));
    const cover=el('button','card-cover');cover.setAttribute('aria-label','查看 '+item.name);
    if(item.cover_url){const img=el('img');img.src=item.cover_url;img.alt='';img.loading='lazy';cover.append(img);}
    else{const placeholder=el('span','placeholder');placeholder.append(icon('image-plus'),el('span','','添加例图'));cover.append(placeholder);}
    cover.append(el('span','serial','N° '+String(index+1).padStart(2,'0')),el('span','example-count',item.example_count+' 张例图'));
    cover.onclick=async()=>{
      if(state.busy||!canLeave())return;
      try{await detail(item.id);$('workspace').scrollIntoView({block:'start'});}catch(err){report(err);}
    };
    const body=el('div','card-body');const name=el('h3','card-title',item.name);name.title=item.name;
    const heading=el('div','card-heading');heading.append(name,modelBadge(item.cover_model));
    const desc=el('p','card-desc',item.description||'可在工作台补充画风说明');desc.title=item.description||'';
    const copy=el('button','card-copy');copy.append(icon('copy'),el('span','','复制画风串'));copy.setAttribute('aria-label','复制画风串 · '+item.name);
    copy.onclick=async()=>{try{const fixed=await RecipeRepair.inspect(item.recipe,true);if(fixed.count)toast(`已修复 ${fixed.count} 处画师 emoji 格式。`);await copyText(fixed.text,copy);}catch(err){report(err);}};
    const generate=el('a','card-generate','用此画风生图');generate.href='/generate?style='+item.id;
    body.append(heading,desc,copy,generate);card.append(cover,body);cards.append(card);
  }
  $('no-results').hidden=Boolean(cards.children.length);
  $('no-results').textContent=state.styles.length?'没有找到匹配的画风。':'收藏库是空的，打开一张 PNG 开始收藏。';
}
$('search').oninput=renderCards;
const copyTimers=new WeakMap();
async function copyText(text,button,label='复制画风串'){
  if(!text)return;
  try{
    try{await navigator.clipboard.writeText(text);}
    catch{
      const active=document.activeElement,field=el('textarea');field.value=text;field.style.cssText='position:fixed;left:-10000px;top:0;';
      document.body.append(field);field.select();const ok=document.execCommand('copy');field.remove();active?.focus({preventScroll:true});
      if(!ok)throw new Error('复制受浏览器限制，请手动复制右侧画风串');
    }
    const span=button.querySelector('span');span.textContent='已复制';
    clearTimeout(copyTimers.get(button));copyTimers.set(button,setTimeout(()=>span.textContent=label,1500));
  }catch(err){report(err);}
}
$('copy').onclick=async()=>{try{const fixed=await recipeEditor.check(true);await copyText(fixed.text,$('copy'),'复制画师串');}catch(err){report(err);}};
$('copy-negative').onclick=()=>copyText(exampleNegative,$('copy-negative'),'复制负面词');
function pick(target=null){
  if(state.busy)return;state.pendingTarget=target;$('file').value='';$('file').click();
}
$('choose').onclick=()=>pick(state.detail?.id??null);
$('open').onclick=()=>pick();$('replace').onclick=()=>pick();
$('file').onchange=()=>{const f=$('file').files[0];if(f)inspectFile(f,state.pendingTarget);};
async function inspectFile(file,target,link=null){
  if(state.busy)return;
  if(file?.size>64*1024*1024){toast('图片超过 64 MB，请选择较小的原图',true);return;}
  if(!canLeave())return;
  ++state.loadId;busy(true);
  clearDraft();state.detail=null;state.example=null;
  $('example-tools').hidden=true;
  putForm({},link?'正在获取原图…':'解析中…');
  let url=file?URL.createObjectURL(file):null;
  preview(url,file?.name||'',{},file?.name||'Discord 附件');
  $('empty-art').hidden=true;
  $('import-message').textContent=$('mode').textContent;
  $('import-status').hidden=false;
  $('workspace').scrollIntoView({block:'start',behavior:'instant'});
  renderCards();
  try{
    if(link){
      try{
        const response=await fetch('/api/discord-attachment',{...jsonOptions('POST',link),signal:AbortSignal.timeout(30000)});
        if(!response.ok)throw new Error((await response.json()).error);
        const blob=await response.blob();
        file=new File([blob],new URL(link).pathname.split('/').pop(),{type:blob.type});
        if(url)URL.revokeObjectURL(url);
        url=URL.createObjectURL(file);
        preview(url,file.name,{},file.name);
      }catch(err){if(!file)throw err;toast('获取附件失败，继续解析拖入的图片');}
    }
    $('mode').textContent=$('import-message').textContent='解析中…';
    const info=await api('/api/inspect',{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file,signal:AbortSignal.timeout(60000)});
    state.draft={file,url,target,hash:info.hash};
    const name=info.name||file.name.replace(/\.(png|webp)$/i,'');
    const mode=({manual:'手动粘贴画风串',suggested:'建议画风串',explicit:'原始画风串'})[info.mode]||'建议画风串';
    putForm({...info,name},mode,info.warnings||[]);
    preview(state.draft.url,file.name,info.metadata||{},file.name);
    $('example-tools').hidden=true;renderCards();$('workspace').scrollIntoView({block:'start'});
    if(info.mode==='manual')toast('未找到生成记录，可手动粘贴画风串');
  }catch(err){
    if(url)URL.revokeObjectURL(url);
    const reason=err.name==='TimeoutError'?'请求超时，请重新拖入原图':err.message||'请重新拖入原图';
    preview(null,'',{},'导入失败');
    $('empty-title').textContent=reason;
    $('mode').textContent='导入失败';toast(reason,true);
  }finally{$('import-status').hidden=true;busy(false);}
}
let dragDepth=0;
const hasFiles=e=>Array.from(e.dataTransfer?.types||[]).some(t=>['Files','text/uri-list','text/html','DownloadURL','text/x-moz-url'].includes(t)||t==='text/plain'&&!e.target.closest?.('textarea,input'));
window.addEventListener('dragenter',e=>{if(!hasFiles(e))return;e.preventDefault();dragDepth++;$('drop').classList.add('over');});
window.addEventListener('dragover',e=>{if(hasFiles(e)){e.preventDefault();e.dataTransfer.dropEffect='copy';}});
window.addEventListener('dragleave',e=>{if(hasFiles(e)&&--dragDepth<=0){dragDepth=0;$('drop').classList.remove('over');}});
window.addEventListener('drop',async e=>{
  if(!hasFiles(e))return;
  e.preventDefault();dragDepth=0;$('drop').classList.remove('over');
  if(state.busy)return;
  const dt=e.dataTransfer;
  let file=dt.files[0];
  const parts=['text/uri-list','DownloadURL','text/x-moz-url','text/plain','text/html'].map(t=>dt.getData(t));
  if(file && /\.url$/i.test(file.name) && file.size<16384){parts.push(await file.text());file=null;}
  const link=parts.join('\n').replace(/&amp;/gi,'&').match(/https:\/\/(?:cdn\.discordapp\.com|media\.discordapp\.net)\/(?:ephemeral-)?attachments\/\d+\/\d+\/[^\s"'<>]+/i)?.[0];
  if(file||link)await inspectFile(file,null,link);
  else toast('请在 Discord 图片右上角下载附件，再把 PNG 或 WebP 文件拖到这里。',true);
});

function targetNote(){
  $('target-note').textContent=$('target').value?'这张例图和原始参数将加入所选画风，沿用该收藏已确认的配方。':'将当前的名称、说明和画风串保存为一套独立配方。';
}
function saveDialog(){
  const match=state.styles.find(s=>normalize(s.recipe)===normalize(valueOf('recipe')));
  const target=$('target');target.replaceChildren(new Option('新建画风收藏',''));
  for(const s of state.styles)target.add(new Option(s.name,String(s.id)));
  target.value=state.draft.target!=null?String(state.draft.target):String(match?.id??'');
  $('match-note').textContent=match?'找到相同的画风串，可直接追加例图。':'选择新建收藏，或把这张图加入已有画风。';
  targetNote();$('save-dialog').showModal();
}
$('target').onchange=targetNote;
$('cancel').onclick=$('dialog-close').onclick=()=>$('save-dialog').close();
function base64(file){
  return new Promise((resolve,reject)=>{const reader=new FileReader();
    reader.onload=()=>resolve(reader.result.slice(reader.result.indexOf(',')+1));
    reader.onerror=()=>reject(new Error('读取原图失败，请重新选择'));reader.readAsDataURL(file);
  });
}
$('confirm').onclick=async()=>{
  if(state.busy||!state.draft)return;
  const target=$('target').value?Number($('target').value):null;
  if(target===null&&!valueOf('name').trim()){$('save-dialog').close();toast('请为新收藏填写名称',true);$('name').focus();return;}
  $('save-dialog').close();busy(true);
  try{
    await recipeEditor.check();
    const draft=state.draft;
    const result=await api('/api/save',jsonOptions('POST',{...fields(),image:await base64(draft.file),filename:draft.file.name,target_id:target}));
    await styles();await detail(result.id);
    const current=state.detail.examples.find(e=>e.image_url?.includes(draft.hash));
    if(current)setExample(current);
    toast(result.duplicate?'已收藏，这张例图已在该配方中':target===null?'新画风已保存':'例图已加入收藏');
  }catch(err){report(err);}finally{busy(false);}
};
$('save').onclick=async()=>{
  if(state.busy)return;
  busy(true);
  try{
    await recipeEditor.check();
    if(state.draft){saveDialog();return;}
    if(!state.detail)return;
    if(!valueOf('name').trim()){toast('请填写画风名称',true);$('name').focus();return;}
    const id=state.detail.id,example=state.example?.id;
    await api('/api/styles/'+id,jsonOptions('PUT',fields()));
    await styles();await detail(id,example);toast('画风修改已保存');
  }catch(err){report(err);}finally{busy(false);}
};
$('set-cover').onclick=async()=>{
  if(state.busy||!state.example||!state.detail)return;
  busy(true);
  try{
    const id=state.example.id;
    state.detail=await api('/api/styles/'+state.detail.id,jsonOptions('PUT',{cover_id:id}));
    setExample(state.detail.examples.find(e=>e.id===id));await styles();toast('已更新封面');
  }catch(err){report(err);}finally{busy(false);}
};
const links=document.querySelectorAll('.nav-link');
function navActive(){links.forEach(a=>a.classList.toggle('active',a.hash==='#workspace'));}
window.addEventListener('hashchange',navActive);navActive();
// A navigation snapshot preserves unsaved fields and the selected original File.
function workbenchSnapshot(write){
  return new Promise((resolve,reject)=>{
    const request=indexedDB.open('style-library-workbench',1);
    request.onupgradeneeded=()=>request.result.createObjectStore('draft');
    request.onerror=()=>reject(request.error);
    request.onsuccess=()=>{
      const db=request.result,tx=db.transaction('draft','readwrite'),store=tx.objectStore('draft');let value;
      if(write)store.put(write,'navigation');else{const get=store.get('navigation');get.onsuccess=()=>{value=get.result;store.delete('navigation');};}
      tx.oncomplete=()=>{db.close();resolve(value);};tx.onerror=()=>{db.close();reject(tx.error);};
    };
  });
}
document.addEventListener('click',async event=>{
  const link=event.target.closest('a[href^="/generate"]');if(!link||event.ctrlKey||event.metaKey||event.shiftKey)return;
  event.preventDefault();if(state.busy){toast('原图正在解析，请稍候再切换。');return;}
  try{
    await workbenchSnapshot({detailId:state.detail?.id,exampleId:state.example?.id,fields:fields(),original:state.original,
      draft:state.draft?{file:state.draft.file,target:state.draft.target,hash:state.draft.hash}:null,
      metadata:currentMetadata,mode:$('mode').textContent,warnings:$('warnings').textContent});
    location.assign(link.href);
  }catch(err){report(new Error('草稿暂存失败，请保存当前编辑后再切换。'));}
});
(async()=>{
  busy(true);
  try{
    const saved=await workbenchSnapshot();await styles();
    if(saved?.detailId&&state.styles.some(s=>s.id===saved.detailId))await detail(saved.detailId,saved.exampleId);
    else if(!saved?.draft&&state.styles.length)await detail((state.styles.find(s=>s.cover_url)||state.styles[0]).id);
    else metadata();
    if(saved){
      if(saved.draft){clearDraft();state.detail=null;state.example=null;state.draft={...saved.draft,url:URL.createObjectURL(saved.draft.file)};preview(state.draft.url,state.draft.file.name,saved.metadata,state.draft.file.name);$('example-tools').hidden=true;}
      putForm(saved.fields,saved.mode,saved.warnings?[saved.warnings]:[]);state.original=saved.original;syncForm();
    }
  }
  catch(err){report(err);}finally{busy(false);recipeEditor.check().catch(report);}
})();
