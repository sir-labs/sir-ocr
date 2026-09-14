const $=id=>document.getElementById(id);
const params=new URLSearchParams(location.hash.slice(1));
const id=params.get('job'),token=params.get('token');
let pages=[],selected=null,images={},sequence=0;
async function api(path){const r=await fetch(path,{headers:{Authorization:`Bearer ${token}`}});if(!r.ok){let text=`HTTP ${r.status}`;try{text=(await r.json()).detail||text;}catch{}throw new Error(text);}return r.json();}
// Only explicit image references from this authenticated page may load.
DOMPurify.addHook('uponSanitizeAttribute',(_node,data)=>{
  if(data.attrName==='src'){if(Object.hasOwn(images,data.attrValue))data.attrValue=images[data.attrValue];else data.keepAttr=false;}
  if(['href','srcset','style','id','name'].includes(data.attrName))data.keepAttr=false;
});
function render(markdown){
  const math=[];const prefix=`SIRMATH${crypto.randomUUID().replaceAll('-','')}X`;
  const protectedText=markdown.split(/(```[\s\S]*?```|`[^`\n]*`)/g).map((part,index)=>index%2?part:part.replace(/\$\$([\s\S]*?)\$\$|(?<!\$)\$(?!\$)([^$\n]+)\$(?!\$)/g,(_all,block,inline)=>{const key=prefix+math.length+'END';math.push({tex:block??inline,display:block!==undefined,key});return key;})).join('');
  const clean=DOMPurify.sanitize(marked.parse(protectedText),{RETURN_DOM_FRAGMENT:true,ALLOWED_TAGS:['p','br','hr','h1','h2','h3','h4','h5','h6','strong','em','del','ul','ol','li','blockquote','pre','code','table','thead','tbody','tfoot','tr','th','td','img','span','div','sup','sub','a'],ALLOWED_ATTR:['src','alt','title','colspan','rowspan']});
  $('document').replaceChildren(clean);
  const walker=document.createTreeWalker($('document'),NodeFilter.SHOW_TEXT),nodes=[];while(walker.nextNode())nodes.push(walker.currentNode);
  const pattern=new RegExp(prefix+'(\\d+)END','g');
  for(const node of nodes){if(!node.textContent.includes(prefix))continue;const fragment=document.createDocumentFragment();let from=0;for(const match of node.textContent.matchAll(pattern)){fragment.append(document.createTextNode(node.textContent.slice(from,match.index)));const span=document.createElement('span'),entry=math[Number(match[1])];katex.render(entry.tex,span,{displayMode:entry.display,throwOnError:false,trust:false,maxExpand:1000,maxSize:20,strict:'ignore'});fragment.append(span);from=match.index+match[0].length;}fragment.append(document.createTextNode(node.textContent.slice(from)));node.replaceWith(fragment);}
}
async function show(number){const request=++sequence;selected=number;$('page').value=String(number);$('previous').disabled=pages.indexOf(number)<=0;$('next').disabled=pages.indexOf(number)>=pages.length-1;$('reader-error').textContent='';$('document').textContent='กำลังโหลดหน้า…';$('source').textContent='';try{const d=await api(`/api/jobs/${id}/preview/pages/${number}`);if(request!==sequence)return;images=d.images;render(d.markdown);$('source').textContent=d.markdown;}catch(e){if(request===sequence){$('document').replaceChildren();$('reader-error').textContent=e.message;}}}
async function load(){try{const d=await api(`/api/jobs/${id}`);pages=d.pages.filter(p=>p.state==='done').map(p=>p.number);$('reader-status').textContent=`อ่านได้ ${pages.length} / ${d.page_count} หน้า${d.state==='cancelled'?' · ยกเลิกงานแล้ว':d.state==='completed'?' · เสร็จแล้ว':' · อัปเดตเพื่อดูหน้าที่เสร็จเพิ่ม'}`;$('page').replaceChildren(...pages.map(n=>{const o=document.createElement('option');o.value=n;o.textContent=String(n);return o;}));if(pages.length)await show(pages.includes(selected)?selected:pages[0]);else{$('document').textContent='ยังไม่มีหน้าที่เสร็จ กรุณากลับไปดูสถานะงาน';$('previous').disabled=$('next').disabled=true;}}catch(e){$('reader-error').textContent=e.message;$('reader-status').textContent='เปิดผลไม่ได้';}}
$('page').onchange=()=>show(Number($('page').value));$('previous').onclick=()=>show(pages[pages.indexOf(selected)-1]);$('next').onclick=()=>show(pages[pages.indexOf(selected)+1]);$('reload').onclick=load;
if(/^[a-f0-9]{32}$/.test(id||'')&&/^[A-Za-z0-9_-]{43}$/.test(token||'')){$('back').href='/'+location.hash;load();}else{$('reader-error').textContent='ลิงก์งานไม่ถูกต้อง กรุณาเปิดจากหน้าสถานะงาน';$('reader-status').textContent='';}
