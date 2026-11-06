'use strict';
const $=id=>document.getElementById(id);
const node=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!=null)n.textContent=text;return n;};
const prefix='/api/generation/';
const state={draft:null,caps:null,styles:[],selectedDetail:null,image:null,imageLoading:false,history:[],job:null,submitting:false,uploadBusy:0,view:'result',initialized:false};
const numeric=new Set(['width','height','steps','guidance','rescale','uc_strength','count','strength','noise','reference_strength','reference_fidelity']);
let toastTimer,draftTimer,previewTimer,pollTimer,previewVersion=0,imageVersion=0,settingsClear={};
let draftSaveQueue=Promise.resolve();
async function api(path,body,method){
  const response=await fetch(path.startsWith('/')?path:prefix+path,{cache:'no-store',...(body!==undefined?{method:method||'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})});
  let data;try{data=await response.json();}catch{throw new Error('本地服务返回了无法读取的响应，请检查服务是否已启动。');}
  if(!response.ok)throw new Error(data.error||'请求失败');return data;
}
function toast(message,error=false){$('toast').textContent=message;$('toast').className='toast show'+(error?' error':'');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').className='toast',error?7000:3000);}
function report(error){toast(error.message||String(error),true);}
function current(){
  const d={...state.draft};
  for(const field of document.querySelectorAll('[data-field]')){
    if(field.type==='checkbox')d[field.id]=field.checked;
    else if(numeric.has(field.id))d[field.id]=field.value===''?'':Number(field.value);
    else {const old=String(d[field.id]??'');d[field.id]=field.value===old.replace(/\r\n?/g,'\n')?d[field.id]:field.value;}
  }
  return d;
}
function fill(){
  for(const field of document.querySelectorAll('[data-field]')){
    if(field.type==='checkbox')field.checked=Boolean(state.draft[field.id]);else field.value=state.draft[field.id]??'';
  }
  updateControls();renderSelection();renderBase();renderAnalysis();updateMode();renderPrevious();
}
function writeDraft(draft){
  const snapshot=structuredClone(draft);
  const saving=draftSaveQueue.catch(()=>{}).then(()=>api('draft',snapshot,'PUT'));
  draftSaveQueue=saving;return saving;
}
function saveDraft(){if(!state.initialized)return;state.draft=current();clearTimeout(draftTimer);draftTimer=setTimeout(()=>writeDraft(current()).catch(report),500);}
async function persistDraft(){if(!state.draft)return;clearTimeout(draftTimer);state.draft=current();await writeDraft(state.draft);}
window.addEventListener('pagehide',()=>{if(state.initialized)fetch(prefix+'draft',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(current()),keepalive:true}).catch(()=>{});});
document.querySelectorAll('a[href="/"]').forEach(link=>link.addEventListener('click',async event=>{
  if(event.ctrlKey||event.metaKey||event.shiftKey||!state.initialized)return;
  event.preventDefault();try{await persistDraft();location.assign('/');}catch(e){report(e);}
}));
for(const field of document.querySelectorAll('[data-field]')){
  field.addEventListener('input',()=>{
    if(['width','height','fit'].includes(field.id))invalidateMask();
    updateControls();saveDraft();schedulePreview();
  });
}
function invalidateMask(){if(state.draft?.mask){state.draft.mask='';$('mask-status').textContent='画布已改变，请重新绘制遮罩。';}}
function isRunning(){return ['queued','composing','running','stopping'].includes(state.job?.state);}
function updateControls(){
  if(!state.draft)return;
  const m=state.caps.models.find(x=>x.id===$('model').value);
  const v5=$('model').value.startsWith('nai-diffusion-5');
  $('effort').textContent=v5?'HIGH':'V4.5';
  $('model-note').textContent=v5?'高模式 · 完整参数 · Karras 噪声调度':'完整参数 · 支持人物与画风精确参考';
  $('schedule').disabled=!m?.schedule;if(!m?.schedule)$('schedule').value='karras';
  $('reference-fields').disabled=!m?.reference;
  if(!m?.reference)$('use_reference').checked=false;
  $('reference-note').textContent=m?.reference?'精确参考使用 V4.5，每张参考图有额外费用，实际计费以中转为准。':'当前 V5 暂未开放精确参考。切换 V4.5 后可启用，参考图会保留。';
  const w=Number($('width').value),h=Number($('height').value);
  const valid=w>=64&&h>=64&&w<=4096&&h<=4096&&w%64===0&&h%64===0&&w*h<=3145728;
  $('size-error').hidden=valid;$('size-error').querySelector('span').textContent='宽高需为 64 的倍数、单边至多 4096、总像素至多 3,145,728。';
  $('pixel-count').textContent=Number.isFinite(w*h)?(w*h/1e6).toFixed(2)+' MP':'—';
  const size=w+','+h;$('size-preset').value=[...$('size-preset').options].some(o=>o.value===size)?size:'custom';
  $('generate-count').textContent=$('count').value+' 张';
  $('advanced-status').textContent='整理时纳入质量标签：'+($('quality').value==='none'?'关闭':'标准（纳入当次发送文本）')+' · 负面预设：'+($('uc_preset').value==='none'?'关闭':'Heavy（加入负面开头）');
  $('generate').disabled=isRunning()||state.submitting||state.imageLoading||Boolean(state.uploadBusy)||!valid||!($('request').value.trim()||$('recipe').value.trim()||state.draft.analysis_image);
  $('reroll').disabled=isRunning()||state.submitting||state.imageLoading||Boolean(state.uploadBusy)||!valid||!state.image?.reroll?.available;
  $('reroll-note').hidden=state.view!=='result'||state.imageLoading||!state.image||Boolean(state.image.reroll?.available);
  $('reroll-note').textContent=state.image?.reroll?.reason||'请刷新页面后重新选择历史图片。';
  $('stop').hidden=!isRunning();$('stop').disabled=state.job?.state==='stopping';
  $('stop').textContent=state.job?.state==='stopping'?'正在停止…':['queued','composing'].includes(state.job?.state)?'停止本次生成':'停止后续图片';
  $('analysis-current').disabled=!state.image&&!state.draft.base;
  $('view-analysis').disabled=!state.draft.analysis_image;
  $('view-base').disabled=!state.draft.base;
  $('view-result').disabled=!state.image;
}
function renderPrevious(){
  const text=state.draft?.final_prompt||'';
  $('previous-record').hidden=!text;
  $('previous-text').textContent=text;
}
async function copyRecord(button,text){
  try{await navigator.clipboard.writeText(text||'');const before=button.textContent;button.textContent='已复制';setTimeout(()=>button.textContent=before,1500);}catch{toast('复制失败，请选中文本手动复制。',true);}
}
$('copy-previous').onclick=e=>copyRecord(e.currentTarget,state.draft?.final_prompt);
$('copy-actual').onclick=e=>copyRecord(e.currentTarget,state.image?.snapshot.sent_prompt??state.image?.snapshot.final_prompt);
$('copy-job').onclick=e=>copyRecord(e.currentTarget,state.job?.record?.final_prompt);
function pane(name){document.querySelector('.studio').dataset.mobile=name;document.querySelectorAll('[data-pane]').forEach(b=>b.classList.toggle('active',b.dataset.pane===name));}
document.querySelectorAll('[data-pane]').forEach(b=>b.onclick=()=>pane(b.dataset.pane));
document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>$(b.dataset.close).close());
document.querySelectorAll('[data-copy]').forEach(b=>b.onclick=async()=>{try{await navigator.clipboard.writeText(current()[b.dataset.copy]||'');const old=b.textContent;b.textContent='已复制';setTimeout(()=>b.textContent=old,1500);}catch{toast('复制失败，请选中文本手动复制。',true);}});
$('size-preset').onchange=()=>{if($('size-preset').value==='custom')return;const [w,h]=$('size-preset').value.split(',');$('width').value=w;$('height').value=h;invalidateMask();updateControls();saveDraft();schedulePreview();};
$('swap-size').onclick=()=>{const w=$('width').value;$('width').value=$('height').value;$('height').value=w;invalidateMask();updateControls();saveDraft();schedulePreview();};
$('nearest-size').onclick=async()=>{try{const size=await api('nearest-size',{width:Number($('width').value)||1024,height:Number($('height').value)||1024});$('width').value=size.width;$('height').value=size.height;invalidateMask();updateControls();saveDraft();schedulePreview();}catch(e){report(e);}};
function updateMode(){
  const mode=state.draft.mode;
  document.querySelectorAll('[data-mode]').forEach(b=>b.classList.toggle('active',b.dataset.mode===mode));
  $('base-controls').hidden=mode==='generate';$('edit-mask').hidden=mode!=='infill';$('mask-status').hidden=mode!=='infill';
  if(mode==='infill'){
    const fallback=$('model').value==='nai-diffusion-5-curated';
    $('mask-status').textContent=(state.draft.mask?'遮罩已保存。':'请绘制重绘区域。')+(fallback?' V5 Curated 的局部重绘使用 V4.5 Curated。':'');
  }
}
document.querySelectorAll('[data-mode]').forEach(b=>b.onclick=()=>{state.draft=current();state.draft.mode=b.dataset.mode;updateMode();saveDraft();schedulePreview();if(b.dataset.mode!=='generate'&&state.draft.base)showBase();});
$('model').addEventListener('change',()=>{updateMode();});
function renderSelection(){
  const item=state.styles.find(x=>x.id===state.draft.style_id);
  $('selected-style').hidden=!state.draft.style_id;
  $('selected-name').textContent=state.draft.style_name||item?.name||'已关联画风';
  $('selected-example').textContent=state.draft.example_id?'例图 #'+state.draft.example_id+' · 可在此自由调整':'独立画师串快照';
  $('selected-cover').hidden=!item?.cover_url;if(item?.cover_url)$('selected-cover').src=item.cover_url;
}
async function refreshStyles(){state.styles=(await api('/api/styles')).styles;}
let pickerDetail=null;
function renderStyles(){
  const query=$('style-search').value.trim().toLowerCase();const list=$('style-list');list.replaceChildren();
  for(const s of state.styles){if(![s.name,s.recipe,s.description].some(v=>(v||'').toLowerCase().includes(query)))continue;
    const b=node('button','style-choice'+(pickerDetail?.id===s.id?' selected':''));
    if(s.cover_url){const img=node('img');img.src=s.cover_url;img.alt='';img.loading='lazy';b.append(img);}else b.append(node('span','no-cover','▧'));
    b.append(node('strong','',s.name));b.onclick=()=>chooseStyle(s.id).catch(report);list.append(b);
  }
  if(!list.children.length)list.append(node('p','hint','没有匹配的画风。'));
}
async function openStyles(){try{await refreshStyles();pickerDetail=null;$('style-example-select').hidden=true;$('style-search').value='';renderStyles();$('style-dialog').showModal();}catch(e){report(e);}}
$('style-open').onclick=openStyles;$('canvas-style').onclick=openStyles;$('style-search').oninput=renderStyles;
async function chooseStyle(id){
  pickerDetail=await api('/api/styles/'+id);renderStyles();$('style-example-select').hidden=false;
  $('picker-name').textContent=pickerDetail.name;$('picker-example').replaceChildren();
  for(const ex of pickerDetail.examples){const option=node('option','',ex.filename+' · '+(ex.metadata.model||ex.metadata.source||'模型未记录'));option.value=ex.id;$('picker-example').append(option);}
  if(!pickerDetail.examples.length){const option=node('option','','无例图 · 使用当前参数');option.value='';$('picker-example').append(option);}
  $('picker-example').value=pickerDetail.cover_id||pickerDetail.examples[0]?.id||'';
  pickerNote();$('style-example-select').scrollIntoView({block:'nearest'});
}
function pickerNote(){const ex=pickerDetail?.examples.find(x=>x.id===Number($('picker-example').value));const m=ex?.metadata||{};$('picker-note').textContent=`${m.steps??'未记录'} 步 · Guidance ${m.guidance??'未记录'} · Rescale ${m.rescale??'未记录'}。带入后尺寸保持当前值，种子设为随机。`;}
$('picker-example').onchange=pickerNote;
function modelId(text){const t=String(text||'').toLowerCase();const version=/4[.\s-]?5/.test(t)?'4-5':/\b5\b|v5/.test(t)?'5':null;return version?'nai-diffusion-'+version+'-'+(/curated/.test(t)?'curated':'full'):null;}
async function applyStyle(detail,ex){
  state.draft=current();const d=state.draft,m=ex?.metadata||{};
  Object.assign(d,{style_id:detail.id,style_name:detail.name,example_id:ex?.id??null,recipe:detail.recipe,negative:m.negative??'',seed:''});
  const id=modelId(m.model||m.source);if(id)d.model=id;
  for(const key of ['sampler','steps','guidance','rescale','uc_strength','schedule'])if(m[key]!==undefined&&m[key]!==null&&m[key]!=='')d[key]=m[key];
  if(!state.caps.samplers[d.sampler])d.sampler='k_euler_ancestral';
  d.schedule=String(d.schedule||'karras').toLowerCase();if(!state.caps.schedules.includes(d.schedule))d.schedule='karras';
  d.quality='none';d.uc_preset='none';fill();await persistDraft();schedulePreview();toast('画风已带入，可修改画师串和负面词，然后点击生成图片。');
}
$('apply-style').onclick=async()=>{try{await applyStyle(pickerDetail,pickerDetail.examples.find(x=>x.id===Number($('picker-example').value)));$('style-dialog').close();}catch(e){report(e);}};
$('style-clear').onclick=()=>{state.draft=current();Object.assign(state.draft,{style_id:null,style_name:'',example_id:null});renderSelection();saveDraft();};

async function preview(){
  const version=++previewVersion;
  try{const data=await api('preview',current());if(version!==previewVersion)return;$('final-request').textContent=JSON.stringify(data,null,2);}
  catch(e){if(version===previewVersion)$('final-request').textContent=e.message;}
}
function schedulePreview(){if(!$('request-preview').open)return;clearTimeout(previewTimer);previewTimer=setTimeout(preview,350);}
$('request-preview').ontoggle=()=>{if($('request-preview').open)preview();};$('refresh-preview').onclick=preview;

async function settings(){
  try{const data=await api('settings');settingsClear={};for(const k of ['nai','assistant']){$(k+'-url').value=data[k].url;$(k+'-key').value='';$(k+'-key').placeholder=data[k].has_key?'••••••••（已保存，留空保留）':'填入 API Key';$(k+'-key-state').textContent=data[k].has_key?'已加密保存':'尚未填写密钥';} $('assistant-model').value=data.assistant.model;$('settings-status').textContent='';$('settings-dialog').showModal();}catch(e){report(e);}
}
$('settings-open').onclick=settings;
async function saveSettings(){
  const payload={};for(const k of ['nai','assistant'])payload[k]={url:$(k+'-url').value,key:$(k+'-key').value,clear_key:!!settingsClear[k]};payload.assistant.model=$('assistant-model').value;
  const result=await api('settings',payload,'PUT');settingsClear={};
  for(const k of ['nai','assistant']){$(k+'-key').value='';$(k+'-key').placeholder=result[k].has_key?'••••••••（已保存，留空保留）':'填入 API Key';$(k+'-key-state').textContent=result[k].has_key?'已加密保存':'尚未填写密钥';}
  return result;
}
async function settingsAction(button,action){
  button.disabled=true;$('settings-status').className='notice';$('settings-status').textContent='处理中…';
  try{await saveSettings();const result=await action();$('settings-status').textContent=result||'设置已保存。';}
  catch(e){$('settings-status').className='notice error';$('settings-status').textContent=e.message;}
  finally{button.disabled=false;}
}
$('settings-save').onclick=()=>settingsAction($('settings-save'),async()=> '设置已保存。');
for(const k of ['nai','assistant']){$(k+'-clear').onclick=()=>{settingsClear[k]=true;$(k+'-key').value='';$(k+'-key-state').textContent='将在保存后清除';};$(k+'-test').onclick=()=>settingsAction($(k+'-test'),async()=>(await api('test',{provider:k})).text);}
$('load-models').onclick=()=>settingsAction($('load-models'),async()=>{const data=await api('models',{});$('assistant-models').replaceChildren(...data.models.map(id=>{const o=node('option');o.value=id;return o;}));return `已读取 ${data.models.length} 个模型，点击模型名输入框选择，也可手动输入。`;});
function assetURL(asset){return '/generation-assets/'+asset;}
function renderBase(){
  $('base-thumb').hidden=!state.draft.base;
  if(state.draft.base){$('base-thumb').src=assetURL(state.draft.base);$('base-label').textContent='已载入底图';}else $('base-label').textContent='等待底图';
  $('reference-thumb').hidden=!state.draft.reference;if(state.draft.reference)$('reference-thumb').src=assetURL(state.draft.reference);
}
function renderAnalysis(){
  const token=state.draft.analysis_image;
  $('analysis-card').hidden=!token;$('analysis-empty').hidden=Boolean(token);
  if(token)$('analysis-thumb').src=assetURL(token);else $('analysis-thumb').removeAttribute('src');
}
function showCanvas(url,label,dimensions=''){$('canvas-image').src=url;$('canvas-image').alt=label;$('canvas-image').hidden=false;$('canvas-empty').hidden=true;$('canvas-title').textContent=label;$('canvas-dimensions').textContent=dimensions;}
function showBase(){if(!state.draft.base)return;state.view='base';showCanvas(assetURL(state.draft.base),'底图预览');$('result-actions').hidden=true;$('actual-details').hidden=true;$('reroll-note').hidden=true;}
function showAnalysis(){if(!state.draft.analysis_image)return;state.view='analysis';showCanvas(assetURL(state.draft.analysis_image),'AI 参考图');$('result-actions').hidden=true;$('actual-details').hidden=true;$('reroll-note').hidden=true;}
$('view-analysis').onclick=$('analysis-view').onclick=showAnalysis;
$('analysis-to-base').onclick=async()=>{if(!state.draft.analysis_image)return;state.draft=current();Object.assign(state.draft,{base:state.draft.analysis_image,mask:'',mode:'img2img'});fill();showBase();try{await persistDraft();toast('已启用底图重绘，可调整 Strength 和 Noise。');}catch(e){report(e);}};
$('analysis-remove').onclick=()=>{state.draft=current();state.draft.analysis_image='';fill();saveDraft();if(state.view==='analysis'){if(state.image)displayImage(state.image);else if(state.draft.base)showBase();else{$('canvas-image').hidden=true;$('canvas-empty').hidden=false;$('canvas-title').textContent='创作画布';}}};
$('view-base').onclick=showBase;
$('view-result').onclick=()=>{if(state.image)displayImage(state.image);};
async function upload(data){const r=await fetch(prefix+'upload',{method:'POST',body:data});const result=await r.json();if(!r.ok)throw new Error(result.error||'图片上传失败');return result;}
async function importFile(file,purpose='analysis'){
  if(!file)return;
  if(file.size>64*1024*1024){toast('图片超过 64 MB，请选择较小的文件。',true);return;}
  if(state.uploadBusy){toast('上一张图片正在载入，请稍候。');return;}
  const url=URL.createObjectURL(file);state.uploadBusy++;updateControls();
  if(purpose!=='reference'){showCanvas(url,purpose==='analysis'?'AI 参考图':'底图预览');$('canvas-progress').hidden=false;$('progress-text').textContent='正在载入图片…';pane('result');}else{$('reference-thumb').src=url;$('reference-thumb').hidden=false;}
  try{const result=await upload(file);state.draft=current();if(purpose==='reference'){state.draft.reference=result.asset;if(!state.caps.models.find(x=>x.id===state.draft.model)?.reference)toast('参考图已保存，切换 V4.5 后可启用。');else state.draft.use_reference=true;}else if(purpose==='base'){state.draft.base=result.asset;state.draft.mask='';state.draft.mode='img2img';}else state.draft.analysis_image=result.asset;
    fill();if(purpose==='base'){showBase();$('base-label').textContent=result.width+' × '+result.height;}else if(purpose==='analysis')showAnalysis();await persistDraft();
  }catch(e){report(e);renderBase();renderAnalysis();if(purpose!=='reference'){if(state.image)displayImage(state.image);else if(state.draft.analysis_image)showAnalysis();else if(state.draft.base)showBase();else{$('canvas-image').hidden=true;$('canvas-empty').hidden=false;}}}
  finally{URL.revokeObjectURL(url);state.uploadBusy--;if(!isRunning())$('canvas-progress').hidden=true;updateControls();}
}
$('base-upload').onclick=()=>{$('base-file').value='';$('base-file').click();};
$('analysis-upload').onclick=$('canvas-upload').onclick=()=>{$('analysis-file').value='';$('analysis-file').click();};
$('reference-upload').onclick=()=>{$('reference-file').value='';$('reference-file').click();};
$('analysis-file').onchange=()=>importFile($('analysis-file').files[0]);$('base-file').onchange=()=>importFile($('base-file').files[0],'base');$('reference-file').onchange=()=>importFile($('reference-file').files[0],'reference');
$('base-remove').onclick=()=>{state.draft=current();Object.assign(state.draft,{base:'',mask:'',mode:'generate'});fill();saveDraft();if(state.draft.analysis_image)showAnalysis();else if(state.image)displayImage(state.image);else{$('canvas-image').hidden=true;$('canvas-empty').hidden=false;$('canvas-title').textContent='创作画布';}};
for(const e of ['dragenter','dragover'])$('canvas').addEventListener(e,event=>{event.preventDefault();$('canvas').classList.add('dragging');});
$('canvas').addEventListener('dragleave',()=>{$('canvas').classList.remove('dragging');});
$('canvas').addEventListener('drop',event=>{event.preventDefault();$('canvas').classList.remove('dragging');const f=event.dataTransfer.files[0];if(f)importFile(f);else toast('请将原图下载到本机，再拖入画布。',true);});
window.addEventListener('dragover',e=>e.preventDefault());window.addEventListener('drop',e=>{if(!$('canvas').contains(e.target))e.preventDefault();});

function displayImage(entry){
  state.image=entry;state.view='result';const d=entry.snapshot;
  showCanvas(entry.url,d.style_name||'生成结果',d.width+' × '+d.height);$('result-actions').hidden=false;$('actual-details').hidden=false;
  $('actual-json').textContent=JSON.stringify({生成方式:d.generation_kind==='reroll'?'历史提示词重 Roll':'AI 整理后生成',来源历史图:d.source_image_id,中文需求:d.request,原始画师串:d.recipe,AI参考图:d.analysis_image,整理时素材:d.composition?.source,AI整理结果:d.composition?.ai_prompt,处理说明:d.composition?.notes,最终生图提示词:d.final_prompt,实际发送正向:d.sent_prompt??d.final_prompt,完整发送负面:d.final_negative,模型:d.actual_model||d.model,参数:{sampler:d.sampler,steps:d.steps,guidance:d.guidance,rescale:d.rescale,负面强度:d.uc_strength,噪声调度:d.schedule,seed:d.seed,width:d.width,height:d.height},图像引导:{模式:d.mode,底图:d.base,适配:d.fit,strength:d.strength,noise:d.noise,遮罩:d.mask,启用参考:d.use_reference,参考类型:d.reference_type,影响强度:d.reference_strength,还原度:d.reference_fidelity}},null,2);
  $('download').href=entry.url;$('download').download='NAI-'+entry.id+'.png';updateControls();renderHistory();
}
function applyHistorySettings(entry){
  const saved=entry.snapshot||{},meta=entry.metadata||{},defaults=state.caps.defaults;
  const present=value=>value!==undefined&&value!==null&&value!=='';
  const value=key=>present(saved[key])?saved[key]:present(meta[key])?meta[key]:defaults[key];
  const modelText=saved.actual_model||meta.model||saved.model||meta.source;
  const model=state.caps.models.find(m=>m.id===modelText)?.id||modelId(modelText)||defaults.model;
  const patch={model,uc_preset:'none'};
  for(const key of ['width','height','steps','guidance','rescale','uc_strength']){
    const number=Number(value(key));patch[key]=Number.isFinite(number)?number:defaults[key];
  }
  patch.sampler=state.caps.samplers[value('sampler')]?value('sampler'):defaults.sampler;
  const schedule=String(value('schedule')).toLowerCase();
  patch.schedule=state.caps.schedules.includes(schedule)?schedule:defaults.schedule;
  patch.negative=[saved.final_negative,meta.negative,saved.negative].find(v=>typeof v==='string')??'';
  state.draft=current();
  const clearedMask=Boolean(state.draft.mask)&&(Number(state.draft.width)!==patch.width||Number(state.draft.height)!==patch.height);
  if(clearedMask)invalidateMask();
  Object.assign(state.draft,patch);fill();
  if(clearedMask){$('mask-status').textContent='历史图尺寸已变化，请重新绘制遮罩。';toast('已同步参数和负面词；尺寸已变化，请重新绘制遮罩。');}
  schedulePreview();
}
async function selectImage(id,{syncSettings=false}={}){
  // An automatic result preview waits for a user's in-progress history selection.
  if(!syncSettings&&state.imageLoading)return false;
  const version=++imageVersion;state.imageLoading=true;updateControls();
  try{
    const entry=await api('images/'+id);await loadImage(entry.url);
    if(version!==imageVersion)return false;
    if(syncSettings)applyHistorySettings(entry);
    displayImage(entry);
    if(syncSettings)await persistDraft();
    return version===imageVersion;
  }catch(error){if(version===imageVersion)throw error;return false;}
  finally{if(version===imageVersion){state.imageLoading=false;updateControls();}}
}
function renderHistory(){
  $('history-count').textContent=state.history.length;$('history-empty').hidden=state.history.length>0;const list=$('history-list');list.replaceChildren();
  for(const h of state.history){const b=node('button','history-card'+(state.image?.id===h.id?' active':''));b.setAttribute('aria-label','查看生成图片并同步参数 '+new Date(h.created*1000).toLocaleString());b.title='同步模型、尺寸、采样参数和负面词；保留种子与创作素材';const img=node('img');img.src=h.url;img.alt=h.style_name||'生成图';img.loading='lazy';b.append(img,node('span','',new Date(h.created*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})+' · '+(h.model.includes('-5-')&&!h.model.includes('-4-5-')?'V5':'V4.5')));b.onclick=()=>selectImage(h.id,{syncSettings:true}).then(shown=>{if(shown)pane('result');}).catch(report);list.append(b);}
}
async function history(){state.history=(await api('history')).images;renderHistory();}
$('history-refresh').onclick=()=>history().catch(report);
$('reuse').onclick=async e=>{e.preventDefault();if(!state.image)return;try{state.draft={...state.caps.defaults,...state.image.snapshot,count:1};fill();await persistDraft();schedulePreview();pane('settings');toast('已套用原始素材、参数与种子，点击生成会重新整理。');}catch(err){report(err);}};
async function useBase(inpaint=false){if(!state.image)return;const response=await fetch(state.image.url);if(!response.ok)throw new Error('读取历史图片失败');const result=await upload(await response.blob());state.draft=current();Object.assign(state.draft,{base:result.asset,mask:'',mode:inpaint?'infill':'img2img'});fill();showBase();await persistDraft();if(inpaint)await openMask();else toast('已用作底图。');}
$('use-base').onclick=()=>useBase().catch(report);$('inpaint-result').onclick=()=>useBase(true).catch(report);
async function useAnalysis(){
  if(state.uploadBusy)return;state.uploadBusy++;updateControls();
  try{let token='';
    if(state.view==='base'&&state.draft.base)token=state.draft.base;
    else if(state.image){const r=await fetch(state.image.url);if(!r.ok)throw new Error('读取当前图片失败');token=(await upload(await r.blob())).asset;}
    else token=state.draft.base;
    if(!token)return;state.draft=current();state.draft.analysis_image=token;fill();showAnalysis();await persistDraft();toast('已作为 AI 参考图，生成模式保持不变。');
  }catch(e){report(e);}finally{state.uploadBusy--;updateControls();}
}
$('analysis-current').onclick=$('use-analysis').onclick=useAnalysis;

function renderJob(){
  const job=state.job;updateControls();const running=isRunning();$('canvas-progress').hidden=!running&&!state.uploadBusy;
  if(!job)return;
  const record=job.record;
  const reroll=job.generation_kind==='reroll';
  $('job-details').hidden=!record?.composition&&!reroll;
  $('job-json').textContent=record&&(record.composition||reroll)?JSON.stringify({生成方式:reroll?'历史提示词重 Roll':'AI 整理后生成',来源历史图:job.source_image_id,中文需求:record.request,画师串:record.recipe,AI参考图:record.analysis_image,本次正向文本:record.final_prompt,原始处理说明:record.composition?.notes,负面提示词:record.negative},null,2):'';
  const progress=`${job.completed} / ${job.total} 张`;
  $('job-status').className=job.error?'error':'';
  if(running){
    const composing=!reroll&&['queued','composing'].includes(job.state);
    $('progress-text').textContent=job.state==='stopping'?'正在等待当前请求结束…':composing?'正在整理提示词…':`正在${reroll?'重 Roll 第':'生成第'} ${Math.min(job.completed+1,job.total)} / ${job.total} 张…`;
    $('job-status').textContent=job.state==='stopping'?'已请求停止，当前请求结束后停止后续步骤。':composing?'AI 正在结合当次需求、参考图和画师串，完成后自动出图。':`已完成 ${progress} · 完成的图片会立即保存。`;
  }else $('job-status').textContent=job.error?`已完成 ${progress}。${job.error}`:job.state==='stopped'?`已停止，已保存 ${progress}。`:job.state==='interrupted'?'任务已中断，请重新点击生成。':`生成完成 · ${progress}`;
}
async function poll(){
  clearTimeout(pollTimer);if(!state.job)return;
  try{const previous=state.job.completed;state.job=(await api('jobs/'+state.job.id)).job;renderJob();if(state.job.completed>previous){await history();const last=state.job.images.at(-1);if(last)await selectImage(last);}if(isRunning())pollTimer=setTimeout(poll,1200);else{await history();}}
  catch(e){$('job-status').textContent='暂时无法读取任务状态，请检查本地服务。'+e.message;pollTimer=setTimeout(poll,3500);}
}
$('generate').onclick=async()=>{
  if(isRunning()||state.submitting||state.imageLoading||state.uploadBusy)return;
  // Freeze exactly the click-time inputs; later edits stay in the next draft.
  const submitted=structuredClone(current());state.submitting=true;updateControls();$('submit-error').hidden=true;
  try{await persistDraft();state.job=(await api('start',submitted)).job;renderJob();pane('result');poll();}
  catch(e){$('submit-error').textContent=e.message;$('submit-error').hidden=false;report(e);}
  finally{state.submitting=false;updateControls();}
};
$('reroll').onclick=async()=>{
  if(isRunning()||state.submitting||state.imageLoading||state.uploadBusy||!state.image?.reroll?.available)return;
  const imageId=state.image.id,submitted=structuredClone(current());
  state.submitting=true;updateControls();$('submit-error').hidden=true;
  try{await persistDraft();state.job=(await api('images/'+imageId+'/reroll',submitted)).job;renderJob();pane('result');poll();}
  catch(e){$('submit-error').textContent=e.message;$('submit-error').hidden=false;report(e);}
  finally{state.submitting=false;updateControls();}
};
$('stop').onclick=async()=>{try{$('stop').disabled=true;state.job=(await api('jobs/'+state.job.id+'/stop',{})).job;renderJob();}catch(e){report(e);updateControls();}};
$('collect').onclick=async()=>{if(!state.image)return;try{await refreshStyles();const select=$('collect-target');select.replaceChildren();const opt=node('option','','新建画风收藏');opt.value='';select.append(opt);for(const s of state.styles){const o=node('option','',s.name);o.value=s.id;select.append(o);}select.value=state.image.snapshot.style_id||'';$('collect-name').value=state.image.snapshot.style_name||'新画风';$('collect-description').value='';$('collect-new').hidden=Boolean(select.value);$('collect-error').hidden=true;$('collect-dialog').showModal();}catch(e){report(e);}};
$('collect-target').onchange=()=>{$('collect-new').hidden=Boolean($('collect-target').value);};
$('collect-confirm').onclick=async()=>{const b=$('collect-confirm');b.disabled=true;try{const result=await api('images/'+state.image.id+'/collect',{target_id:$('collect-target').value?Number($('collect-target').value):null,name:$('collect-name').value,description:$('collect-description').value});$('collect-dialog').close();toast(result.duplicate?'这张图已在该画风收藏中。':'已加入收藏台。');await refreshStyles();}catch(e){$('collect-error').hidden=false;$('collect-error').textContent=e.message;}finally{b.disabled=false;}};

const maskCanvas=document.createElement('canvas');const maskCtx=maskCanvas.getContext('2d',{willReadFrequently:true});
let maskUndo=[],maskErasing=false,maskDrawing=false,lastPoint=null,maskFitWidth=0;
function loadImage(url){return new Promise((resolve,reject)=>{const im=new Image();im.onload=()=>resolve(im);im.onerror=()=>reject(new Error('图片加载失败'));im.src=url;});}
function renderMask(){const ctx=$('mask-overlay').getContext('2d');ctx.clearRect(0,0,maskCanvas.width,maskCanvas.height);ctx.drawImage(maskCanvas,0,0);ctx.globalCompositeOperation='source-in';ctx.fillStyle='rgba(181,148,255,.58)';ctx.fillRect(0,0,maskCanvas.width,maskCanvas.height);ctx.globalCompositeOperation='source-over';$('mask-undo').disabled=!maskUndo.length;}
function maskZoom(){const scale=Number($('mask-zoom').value),w=maskFitWidth*scale;$('mask-stage').style.width=w+'px';$('mask-stage').style.height=w*maskCanvas.height/maskCanvas.width+'px';}
async function openMask(){
  state.draft=current();const d=state.draft;
  if(!d.base){toast('先添加一张底图。');$('base-file').click();return;}
  if(!Number.isInteger(d.width)||!Number.isInteger(d.height)||d.width<64||d.height<64||d.width>4096||d.height>4096||d.width%64||d.height%64||d.width*d.height>3145728){toast('请先设置有效的目标尺寸。',true);return;}
  const image=await loadImage(assetURL(d.base));
  for(const c of [$('mask-base'),$('mask-overlay'),maskCanvas]){c.width=d.width;c.height=d.height;}
  const ctx=$('mask-base').getContext('2d');ctx.fillStyle='#000';ctx.fillRect(0,0,d.width,d.height);
  const ratio=(d.fit==='crop'?Math.max:Math.min)(d.width/image.width,d.height/image.height);const w=image.width*ratio,h=image.height*ratio;ctx.drawImage(image,(d.width-w)/2,(d.height-h)/2,w,h);
  maskCtx.clearRect(0,0,d.width,d.height);maskUndo=[];
  if(d.mask){const old=await loadImage(assetURL(d.mask));if(old.width===d.width&&old.height===d.height){maskCtx.drawImage(old,0,0);const pixels=maskCtx.getImageData(0,0,d.width,d.height);for(let i=0;i<pixels.data.length;i+=4){pixels.data[i+3]=pixels.data[i];pixels.data[i]=pixels.data[i+1]=pixels.data[i+2]=255;}maskCtx.putImageData(pixels,0,0);}}
  $('mask-dialog').showModal();$('mask-zoom').value='1';const viewport=document.querySelector('.mask-viewport');maskFitWidth=Math.min(viewport.clientWidth-24,(viewport.clientHeight-24)*d.width/d.height);maskZoom();renderMask();$('mask-dimensions').textContent=d.width+' × '+d.height+' · 涂色区域重绘';
}
$('edit-mask').onclick=()=>openMask().catch(report);
$('mask-zoom').onchange=maskZoom;
$('brush-size').oninput=()=>{$('brush-value').value=$('brush-size').value;};
function setBrush(erase){maskErasing=erase;$('brush').className=erase?'quiet':'soft active';$('eraser').className=erase?'soft active':'quiet';}
$('brush').onclick=()=>setBrush(false);$('eraser').onclick=()=>setBrush(true);
function checkpoint(){maskUndo.push(maskCtx.getImageData(0,0,maskCanvas.width,maskCanvas.height));if(maskUndo.length>8)maskUndo.shift();}
function position(event){const r=$('mask-overlay').getBoundingClientRect();return {x:(event.clientX-r.left)/r.width*maskCanvas.width,y:(event.clientY-r.top)/r.height*maskCanvas.height};}
function stroke(point){const size=Number($('brush-size').value);maskCtx.globalCompositeOperation=maskErasing?'destination-out':'source-over';maskCtx.strokeStyle='#fff';maskCtx.fillStyle='#fff';maskCtx.lineWidth=size;maskCtx.lineCap='round';maskCtx.lineJoin='round';maskCtx.beginPath();if(lastPoint){maskCtx.moveTo(lastPoint.x,lastPoint.y);maskCtx.lineTo(point.x,point.y);maskCtx.stroke();}else{maskCtx.arc(point.x,point.y,size/2,0,Math.PI*2);maskCtx.fill();}maskCtx.globalCompositeOperation='source-over';lastPoint=point;renderMask();}
$('mask-overlay').onpointerdown=e=>{if(e.button!==0)return;e.preventDefault();checkpoint();maskDrawing=true;lastPoint=null;$('mask-overlay').setPointerCapture(e.pointerId);stroke(position(e));};
$('mask-overlay').onpointermove=e=>{if(maskDrawing)stroke(position(e));};
const finishStroke=()=>{maskDrawing=false;lastPoint=null;};$('mask-overlay').onpointerup=finishStroke;$('mask-overlay').onpointercancel=finishStroke;
$('mask-undo').onclick=()=>{if(maskUndo.length){maskCtx.putImageData(maskUndo.pop(),0,0);renderMask();}};
$('mask-clear').onclick=()=>{checkpoint();maskCtx.clearRect(0,0,maskCanvas.width,maskCanvas.height);renderMask();};
$('mask-apply').onclick=async()=>{const button=$('mask-apply');button.disabled=true;try{const output=document.createElement('canvas');output.width=maskCanvas.width;output.height=maskCanvas.height;const ctx=output.getContext('2d');ctx.fillStyle='#000';ctx.fillRect(0,0,output.width,output.height);ctx.drawImage(maskCanvas,0,0);const blob=await new Promise(resolve=>output.toBlob(resolve,'image/png'));const result=await upload(blob);state.draft=current();state.draft.mask=result.asset;state.draft.mode='infill';updateMode();await persistDraft();$('mask-dialog').close();toast('遮罩已应用。');}catch(e){report(e);}finally{button.disabled=false;}};

async function init(){
  try{
    const [caps,draft,styles,historyData,job]=await Promise.all([api('capabilities'),api('draft'),api('/api/styles'),api('history'),api('job')]);
    state.caps=caps;state.draft=draft.draft;state.styles=styles.styles;state.history=historyData.images;state.job=job.job;
    for(const m of caps.models){const o=node('option','',m.name);o.value=m.id;$('model').append(o);}
    for(const [value,name] of Object.entries(caps.samplers)){const o=node('option','',name);o.value=value;$('sampler').append(o);}
    for(const s of caps.schedules){const o=node('option','',s[0].toUpperCase()+s.slice(1));o.value=s;$('schedule').append(o);}
    fill();state.initialized=true;renderHistory();renderJob();
    const style=Number(new URL(location.href).searchParams.get('style'));
    if(style){const detail=await api('/api/styles/'+style);await applyStyle(detail,detail.examples.find(e=>e.id===detail.cover_id)||detail.examples[0]);historyReplaceURL();}
    if(state.history.length)await selectImage(state.history[0].id);
    if(state.draft.analysis_image)showAnalysis();else if(!state.history.length&&state.draft.base)showBase();
    if(isRunning())poll();
  }catch(e){$('submit-error').hidden=false;$('submit-error').textContent=e.message;report(e);}
}
function historyReplaceURL(){window.history.replaceState(null,'','/generate');}
init();
