/* Shared, capability-protected enrichment UI. No document HTML is inserted here. */
(() => {
  const panel = document.getElementById('classification-panel');
  if (!panel) return;
  panel.innerHTML = `<div class="job-heading"><h2>จัดระเบียบเอกสารนี้</h2><span id="cl-state" class="badge">รอ OCR</span></div>
    <p id="cl-progress" role="status"></p><div id="cl-summary" class="classification-tags"></div>
    <p class="log-note">หมวดและคะแนนด้านบนเป็นผลโมเดลจากข้อความหลัง OCR ทีละหน้า ผลรวมอาจเปลี่ยนเมื่อหน้าอื่นเสร็จ กรุณาตรวจทาน</p><p id="cl-confirmed" hidden></p>
    <div class="actions"><button id="cl-retry" class="secondary" hidden>ลองจัดหมวดหน้าที่ผิดพลาดอีกครั้ง</button><button id="cl-export" class="secondary">ดาวน์โหลดหมวด JSON</button></div>
    <p id="cl-error" class="error" role="alert"></p>
    <p id="cl-login">เข้าสู่ระบบ SIR เพื่อยืนยันหมวดและแนะนำปลายทางส่วนตัว</p>
    <div id="cl-personal" hidden>
      <details><summary>แนะนำโฟลเดอร์จากรายการของฉัน</summary>
        <p class="log-note">ใส่เส้นทางสัมพัทธ์ใน vault หนึ่งโฟลเดอร์ต่อบรรทัด เพิ่มคำอธิบายหลัง | ได้ สูงสุด 30 รายการ ระบบจะแนะนำเท่านั้น ยังไม่ย้ายไฟล์</p>
        <label for="cl-folders">โฟลเดอร์และคำอธิบาย</label><textarea id="cl-folders" rows="5" maxlength="14000" placeholder="Learning/Algorithms | อัลกอริทึมและความซับซ้อน&#10;Learning/ML | Machine learning"></textarea>
        <button id="cl-map" class="secondary">แนะนำปลายทาง</button><p id="cl-map-status" role="status"></p><div id="cl-alternatives" class="classification-tags"></div>
      </details>
      <form id="cl-review"><div class="classification-fields"><label>ประเภทเอกสาร<select id="cl-type"></select></label><label>หัวข้อ<select id="cl-topic"></select></label></div>
      <label for="cl-destination">ปลายทางที่เลือก (แก้ไขได้ / เว้นว่างได้)</label><input id="cl-destination" maxlength="240" placeholder="เส้นทางใน vault ของคุณ">
      <div class="actions"><button type="submit" id="cl-save">ยืนยันหมวดและปลายทาง</button><button type="button" id="cl-copy" class="secondary">คัดลอกเส้นทาง</button></div></form>
      <p id="cl-storage" role="status"></p>
    </div>`;
  const $ = x => document.getElementById('cl-'+x);
  const short = {lecture:'สไลด์ / เอกสารเรียน',research:'บทความวิจัย',exercise:'โจทย์ / แบบฝึกหัด',book:'ตำรา',report:'รายงาน',form:'แบบฟอร์ม / ใบเสร็จ',other:'อื่น ๆ / ไม่ชัดเจน',algorithms:'Algorithms',machine_learning:'Machine Learning',signal_processing:'Signal Processing',software:'Software',mathematics:'คณิตศาสตร์',health:'สุขภาพ',business:'ธุรกิจ',cover:'ปก / สารบัญ',explanation:'คำอธิบาย',definition:'คำนิยาม',proof:'บทพิสูจน์',example:'ตัวอย่าง',references:'อ้างอิง'};
  const labels = {waiting_ocr:'รอ OCR',queued:'รอจัดหมวด',running:'กำลังจัดหมวด',partial:'มีหน้าที่ต้องลองใหม่',done:'จัดหมวดแล้ว',paused:'หยุดตามงาน OCR'};
  let job,token,timer,data,epoch=0,dirty=false,foldersDirty=false,initialized=false;
  const node = (tag,text) => {const e=document.createElement(tag);e.textContent=text;return e;};
  async function call(suffix,body){const r=await fetch(`/api/jobs/${job}/classification${suffix}`,{method:body?'POST':'GET',headers:{Authorization:`Bearer ${token}`,...(body?{'Content-Type':'application/json'}:{})},...(body?{body:JSON.stringify(body)}:{})});let d;try{d=await r.json();}catch{throw new Error('ติดต่อบริการจัดหมวดไม่ได้');}if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);return d;}
  function render(d){
    const review=d.personal?.review;$('confirmed').hidden=!review;$('confirmed').textContent=review?`คุณยืนยัน: ${short[review.document_type]||review.document_type} · ${short[review.topic]||review.topic}${review.destination?' → '+review.destination:''}`:'';
    $('state').textContent=labels[d.state]||d.state;
    $('progress').textContent=`จัดหมวดแล้ว ${d.coverage.classified} / ${d.coverage.total} หน้า${!d.worker_online?' · worker จัดหมวดยังไม่พร้อม (OCR ทำงานต่อได้)':d.worker_state==='loading_model'?' · กำลังโหลดโมเดล':''}`;
    $('retry').hidden=!d.counts.failed;$('export').disabled=!d.coverage.classified;
    $('summary').replaceChildren();for(const kind of ['document_type','topic']){const value=d.summary[kind];if(value){const tag=node('span',`${short[value.label]||value.label} · คะแนนรวม ${(value.scores[value.label]*100).toFixed(0)}%`);tag.className='badge';$('summary').append(tag);}}
    $('personal').hidden=!d.can_personalize;$('login').hidden=d.can_personalize;
    if(!initialized){for(const [id,kind] of [['type','document_type'],['topic','topic']])for(const key of Object.keys(d.catalog[kind])){const o=node('option',short[key]||key);o.value=key;$(id).append(o);}initialized=true;}
    if(!dirty){$('type').value=d.personal?.review?.document_type||d.summary.document_type?.label||'other';$('topic').value=d.personal?.review?.topic||d.summary.topic?.label||'other';$('destination').value=d.personal?.review?.destination||d.personal?.suggestion?.path||'';
      if(!foldersDirty&&document.activeElement!==$('folders'))$('folders').value=(d.personal?.folders||[]).map(f=>f.path+(f.description?' | '+f.description:'')).join('\n');}
    const state=d.personal?.state;$('map-status').textContent=state==='queued'||state==='running'?'กำลังแนะนำปลายทางจากหน้าที่จัดหมวดเสร็จ…':state==='failed'?'แนะนำปลายทางไม่สำเร็จ ลองส่งรายการใหม่':state==='done'?(d.personal.suggestion?.path?'เลือกจากตัวเลือกด้านล่าง หรือแก้เส้นทางเอง':'ยังไม่พบโฟลเดอร์ที่เหมาะสม'):'';
    $('alternatives').replaceChildren();const scores=d.personal?.suggestion?.probabilities||{};
    for(const [path,score] of Object.entries(scores).sort((a,b)=>b[1]-a[1]).slice(0,3)){const b=node('button',`${path==='__none__'?'ไม่มีโฟลเดอร์ที่เหมาะสม':path} · ${(score*100).toFixed(1)}%`);b.className='secondary';b.type='button';b.onclick=()=>{$('destination').value=path==='__none__'?'':path;dirty=true;};$('alternatives').append(b);}
    $('storage').textContent=d.storage==='saved'?'บันทึกในคลังข้อมูลส่วนตัวแล้ว':d.storage==='pending'?'ข้อมูลจัดหมวดกำลังรอบันทึกในคลังส่วนตัว หากบริการไม่พร้อม ระบบจะลองใหม่':'ยังไม่เชื่อมต่อคลังส่วนตัว';
    document.dispatchEvent(new CustomEvent('classification-updated',{detail:{...d,job}}));
  }
  async function poll(){const mine=epoch;clearTimeout(timer);try{const d=await call('');if(mine!==epoch)return;data=d;render(d);$('error').textContent='';
      if(!['done','partial','paused'].includes(d.state)||['queued','running'].includes(d.personal?.state)||(d.can_personalize&&d.storage==='pending'))timer=setTimeout(poll,5000);
    }catch(e){if(mine!==epoch)return;$('error').textContent=e.message;timer=setTimeout(poll,10000);}}
  function start(){const p=new URLSearchParams(location.hash.slice(1));job=p.get('job');token=p.get('token');epoch++;clearTimeout(timer);dirty=false;initialized=false;data=null;$('type').replaceChildren();$('topic').replaceChildren();$('summary').replaceChildren();$('alternatives').replaceChildren();$('folders').value='';$('destination').value='';$('error').textContent='';$('personal').hidden=true;
    panel.hidden=!/^[a-f0-9]{32}$/.test(job||'')||!/^[A-Za-z0-9_-]{43}$/.test(token||'');if(!panel.hidden)poll();}
  $('retry').onclick=async()=>{try{await call('/retry',{});poll();}catch(e){$('error').textContent=e.message;}};
  $('map').onclick=async()=>{const mine=epoch;$('map').disabled=true;try{const folders=$('folders').value.split('\n').filter(l=>l.trim()).map(line=>{const [path,...rest]=line.split('|');return {path:path.trim(),description:rest.join('|').trim()};});await call('/folders',{folders});if(mine===epoch)poll();}catch(e){if(mine===epoch)$('error').textContent=e.message;}finally{$('map').disabled=false;}};
  $('folders').oninput=()=>{foldersDirty=true;};
  $('review').oninput=()=>{dirty=true;};
  $('review').onsubmit=async e=>{e.preventDefault();const mine=epoch;$('save').disabled=true;try{await call('/review',{document_type:$('type').value,topic:$('topic').value,destination:$('destination').value.trim()});if(mine===epoch){dirty=false;poll();}}catch(e){if(mine===epoch)$('error').textContent=e.message;}finally{$('save').disabled=false;}};
  $('copy').onclick=async()=>{try{await navigator.clipboard.writeText($('destination').value);$('storage').textContent='คัดลอกเส้นทางแล้ว';}catch{$('destination').select();}};
  $('export').onclick=()=>{if(!data)return;const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const a=node('a','');a.href=url;a.download='classification.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
  window.addEventListener('hashchange',()=>{foldersDirty=false;start();});start();
})();
