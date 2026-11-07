'use strict';
window.RecipeRepair=(()=>{
  const possible=/[\u200d\ufe0f\u20e3\u2300-\u2bff\u{1f000}-\u{1faff}]/u;
  async function inspect(text,strict=false){
    if(!possible.test(text))return {text,count:0,changes:[],warnings:[]};
    const response=await fetch('/api/recipe/repair',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});
    const result=await response.json();
    if(!response.ok)throw new Error(result.error||'画师串格式检查失败');
    if(strict&&result.warnings.length)throw new Error(result.warnings[0].message);
    return result;
  }
  function bind(field,{read=()=>field.value,onChange=()=>{},notify=()=>{}}={}){
    let version=0;
    field.addEventListener('input',()=>version++);
    async function check(strict=false){
      const before=field.value,text=read(),requestVersion=version;
      const result=await inspect(text,strict);
      if(before!==field.value||requestVersion!==version){
        if(strict)throw new Error('画师串已更新，请再次操作。');
        return null;
      }
      if(result.count){
        field.value=result.text;version++;onChange(result.text);
        notify(`已修复 ${result.count} 处画师 emoji 格式。`);
      }
      if(result.warnings.length)notify(result.warnings[0].message,true);
      return result;
    }
    field.addEventListener('paste',()=>setTimeout(()=>check().catch(error=>notify(error.message,true)),0));
    field.addEventListener('blur',()=>check().catch(error=>notify(error.message,true)));
    return {check};
  }
  return {inspect,bind};
})();
