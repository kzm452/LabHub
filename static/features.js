/* LabHub v5 workspace. All member-supplied strings use textContent, not HTML. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const fmt = ms => new Intl.DateTimeFormat('ja-JP',{timeZone:'Asia/Tokyo',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}).format(new Date(ms));
  const dom = (tag, className='', text='') => { const n=document.createElement(tag); if(className)n.className=className; n.textContent=text; return n; };
  const report = msg => { const t=$('v5-message'); t.textContent=msg; t.hidden=false; clearTimeout(report.timer); report.timer=setTimeout(()=>{t.hidden=true;},5500); };
  let state=null;
  const selectedSlots=[];
  async function authHeader(){
    const head={};
    const key=sessionStorage.getItem('labhub_admin_key');
    if(key)head['X-LabHub-Key']=key;
    if(window.liff){ try {await liff.ready; if(liff.isLoggedIn()){const id=liff.getIDToken(); if(id)head['Authorization']='Bearer '+id;}}catch(_){}}
    return head;
  }
  async function api(url,method='GET',body){
    const headers=await authHeader();
    if(body!==undefined)headers['Content-Type']='application/json';
    const response=await fetch(url,{method,headers,body:body===undefined?undefined:JSON.stringify(body),cache:'no-store'});
    const result=await response.json().catch(()=>({}));
    if(!response.ok)throw new Error(result.error||`HTTP ${response.status}`);
    return result;
  }
  const listEmpty=(target,message)=>{target.replaceChildren(dom('p','v5-empty',message));};
  const asNumber=v=>v===''?null:Number(v);
  const ms=id=>{const value=$(id).value;return value?new Date(value).getTime():NaN;};
  const addText=(parent,cls,txt)=>{const item=dom('span',cls,txt);parent.appendChild(item);return item;};

  function renderEquipment(){
    const list=$('equipment-list');list.replaceChildren();
    if(!state.equipment.length)listEmpty(list,'設備はまだありません。管理者が設備を登録してください。');
    state.equipment.forEach(e=>{
      const row=dom('article','v5-item v54-equipment-row');
      const top=dom('div','v54-booking-primary');
      top.append(dom('strong','',e.name),dom('span','v54-item-type',e.kind==='room'?'会議室':'研究設備'));
      row.append(top,dom('span','v5-muted-text',e.location||'場所未設定'));
      if(e.description)row.append(dom('small','',e.description));
      list.appendChild(row);
    });
    for(const name of ['book-equipment','poll-equipment']){
      const select=$(name);const selected=select.value;select.replaceChildren();
      if(name==='poll-equipment'){const o=dom('option','','指定しない');o.value='';select.append(o);}
      state.equipment.filter(e=>name==='book-equipment'||e.kind==='room').forEach(e=>{const o=dom('option','',e.name);o.value=e.id;select.append(o);});
      if(name==='poll-equipment' && !state.equipment.some(e=>e.kind==='room')) {
        const o=dom('option','','登録された会議室がありません');o.disabled=true;select.append(o);
      }
      if([...select.options].some(o=>o.value===selected))select.value=selected;
    }
    $('equipment-admin').hidden=!state.admin;
    $('book-form').hidden=!state.me;
    const bookings=$('booking-list');bookings.replaceChildren();
    $('v54-reservation-count').textContent=`${state.reservations.length}件`;
    if(!state.reservations.length)listEmpty(bookings,'今後の予約はありません。「＋ 新しい予約を作成」から登録できます。');
    state.reservations.forEach(r=>{
      const row=dom('article','v5-item v54-booking-row');
      const content=dom('div','v54-booking-main');
      const headline=dom('div','v54-booking-primary');
      headline.append(dom('strong','',r.equipment_name));
      content.append(headline,dom('div','v54-booking-time',`${fmt(r.starts_ms)} 〜 ${fmt(r.ends_ms)}`),
        dom('div','v54-booking-owner',`${r.user_name||'研究室メンバー'}${r.purpose?' · '+r.purpose:''}`));
      row.append(content);
      if(r.user_id===state.me||state.admin){
        const b=dom('button','v5-muted v54-cancel','取消');b.type='button';
        b.setAttribute('aria-label',r.equipment_name+'の予約を取消');
        b.onclick=()=>{if(window.confirm(`${r.equipment_name}の予約を取り消しますか？`))action(`/api/v5/reservations/${r.id}/cancel`,'POST',{},'予約を取り消しました');};
        row.append(b);
      }
      bookings.appendChild(row);
    });
    const hist=$('booking-history');hist.replaceChildren();
    if(!state.my_reservations.length)listEmpty(hist,'予約履歴はありません。');
    state.my_reservations.forEach(r=>{const row=dom('div','v5-history',`${fmt(r.starts_ms)} ${r.equipment_name} · ${r.status==='active'?'有効':'取消済み'}`);hist.append(row);});
  }

  const pad=n=>String(n).padStart(2,'0');
  function dateInputValue(date) {
    return `${date.getFullYear()}-${pad(date.getMonth()+1)}-${pad(date.getDate())}`;
  }
  function dateTimeInputValue(date) {
    return `${dateInputValue(date)}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }
  function renderSelectedSlots(){
    const container=$('poll-slot-inputs');container.replaceChildren();
    $('poll-slot-counter').textContent=`${selectedSlots.length} / 12`;
    if(!selectedSlots.length){listEmpty(container,'候補をまだ選択していません。下の「おすすめを探す」から追加できます。');return;}
    selectedSlots.sort((a,b)=>a.starts_ms-b.starts_ms);
    selectedSlots.forEach(slot=>{
      const row=dom('div','v53-selected-slot');
      row.append(dom('span','',`${fmt(slot.starts_ms)} 〜 ${fmt(slot.ends_ms)}`));
      const remove=dom('button','v5-muted','削除');remove.type='button';
      remove.setAttribute('aria-label',`${fmt(slot.starts_ms)}の候補を削除`);
      remove.onclick=()=>{const i=selectedSlots.findIndex(x=>x.starts_ms===slot.starts_ms&&x.ends_ms===slot.ends_ms);
        if(i>=0)selectedSlots.splice(i,1);renderSelectedSlots();};
      row.append(remove);container.append(row);
    });
  }
  function addCandidate(start, end){
    if(!Number.isFinite(start)||!Number.isFinite(end)||end<=start)return report('候補の開始時刻を入力してください');
    if(start<=Date.now())return report('候補日時は未来を指定してください');
    if(selectedSlots.some(x=>x.starts_ms===start&&x.ends_ms===end))return report('同じ候補は追加済みです');
    if(selectedSlots.length>=12)return report('候補日時は12件までです');
    selectedSlots.push({starts_ms:start,ends_ms:end});renderSelectedSlots();
  }
  function renderSuggestions(data){
    const results=$('poll-suggestions');results.replaceChildren();
    $('poll-suggest-note').textContent=data.message||'';
    if(!data.slots.length)return listEmpty(results,'空いている候補が見つかりません。開始日や回答期限、会議室を変更してください。');
    data.slots.forEach(slot=>{
      const line=dom('div','v53-suggestion');
      const label=dom('div','v53-suggestion-desc');
      label.append(dom('strong','',fmt(slot.starts_ms)),dom('small','',`${Math.round((slot.ends_ms-slot.starts_ms)/60000)}分・${data.basis==='historical'?'過去の在室傾向を参考':'設備予約の空き時間から提案'}`));
      const button=dom('button','v5-muted','＋ 追加');button.type='button';
      button.onclick=()=>addCandidate(slot.starts_ms,slot.ends_ms);
      line.append(label,button);results.append(line);
    });
  }
  async function suggest(){
    const button=$('poll-suggest');button.disabled=true;
    $('poll-suggest-note').textContent='日時候補を探しています…';
    try{
      const params=new URLSearchParams({first_day:$('poll-first-day').value,duration:$('poll-duration').value});
      const room=$('poll-equipment').value;if(room)params.set('room_id',room);
      const deadline=ms('poll-deadline');if(Number.isFinite(deadline))params.set('deadline_ms',String(deadline));
      renderSuggestions(await api('/api/v5/schedule-suggestions?'+params.toString()));
    }catch(e){$('poll-suggest-note').textContent=e.message;}finally{button.disabled=false;}
  }
  function renderPolls(){
    const container=$('poll-list');container.replaceChildren();
    if(!state.polls.length)listEmpty(container,'募集中のミーティングはありません。「＋ 新しい日程調整を作成」から登録できます。');
    // Voting requests first, completed meetings later.
    const ordered=[...state.polls].sort((a,b)=>(a.status==='open'?0:1)-(b.status==='open'?0:1)||b.deadline_ms-a.deadline_ms);
    ordered.forEach(p=>{
      const confirmed=p.status!=='open';
      const panel=dom('article','v5-poll v54-poll'+(confirmed?' v54-poll-done':''));
      const head=dom('div','v5-poll-head');
      head.append(dom('strong','',p.title),dom('span','v5-chip',confirmed?'日時確定':'回答受付中'));
      panel.append(head);
      const slots=state.slots.filter(s=>s.poll_id===p.id);
      if(confirmed){
        // Confirmed meetings are informational: do not render disabled voting controls.
        const chosen=slots.find(s=>s.id===p.chosen_slot_id);
        const result=dom('div','v54-confirmed-result');
        result.append(dom('small','', '確定日時'));
        result.append(dom('strong','',chosen?`${fmt(chosen.starts_ms)} 〜 ${fmt(chosen.ends_ms)}`:'確定日時を確認できません'));
        if(p.equipment_name)result.append(dom('span','',`会議室：${p.equipment_name}`));
        panel.append(result);
        if(p.description)panel.append(dom('p','v54-poll-description',p.description));
        const more=dom('details','v54-vote-details');
        more.append(dom('summary','','候補別の回答結果を見る'));
        const results=dom('div','v54-vote-results');
        slots.forEach(s=>results.append(dom('div','v54-vote-row',`${fmt(s.starts_ms)} 〜 ${fmt(s.ends_ms)} · ${s.yes_votes}人`)));
        more.append(results);panel.append(more);
      }else{
        const meta=dom('p','v54-poll-meta',`回答期限 ${fmt(p.deadline_ms)}${p.equipment_name?' · '+p.equipment_name:''}`);
        panel.append(meta);
        if(p.description)panel.append(dom('p','v54-poll-description',p.description));
        const form=dom('form','v5-poll-vote');
        slots.forEach(s=>{
          const label=dom('label','v5-slot');
          const check=dom('input');check.type='checkbox';check.value=s.id;check.checked=s.my_vote===1;
          check.disabled=!state.me||p.deadline_ms<Date.now();
          label.append(check,dom('span','',`${fmt(s.starts_ms)} 〜 ${fmt(s.ends_ms)}`),dom('strong','',`${s.yes_votes}人`));
          form.append(label);
        });
        if(state.me&&p.deadline_ms>=Date.now()){
          const submit=dom('button','v5-muted v54-vote-button','この内容で回答');submit.type='submit';form.append(submit);
          form.onsubmit=async ev=>{ev.preventDefault();const ids=[...form.querySelectorAll('input:checked')].map(c=>Number(c.value));await action(`/api/v5/polls/${p.id}/vote`,'POST',{available_slot_ids:ids},'回答を保存しました');};
        }
        panel.append(form);
        if(p.creator_id===state.me||state.admin){
          const bar=dom('div','v5-finalize');const select=dom('select');
          select.setAttribute('aria-label',`${p.title}の確定日時`);
          slots.slice().sort((a,b)=>b.yes_votes-a.yes_votes||a.starts_ms-b.starts_ms).forEach(s=>{const o=dom('option','',`${fmt(s.starts_ms)}（${s.yes_votes}人）`);o.value=s.id;select.append(o);});
          const btn=dom('button','v5-primary','日時を確定');btn.type='button';
          btn.onclick=()=>{if(window.confirm('この日時に確定しますか？会議室を指定した場合は同時に予約します。'))action(`/api/v5/polls/${p.id}/finalize`,'POST',{slot_id:Number(select.value)},'ミーティングを確定しました');};
          bar.append(select,btn);panel.append(bar);
        }
      }
      container.append(panel);
    });
  }
  function renderWatches(){
    const wrap=$('arrival-watch-list');wrap.replaceChildren();
    const checkbox=$('arrival-sharing');checkbox.disabled=!state.me;
    checkbox.checked=!!state.preferences?.arrival_sharing;
    if(!state.me){listEmpty(wrap,'LINEログイン済みの本人から設定してください。');return;}
    if(!state.people.length){listEmpty(wrap,'ほかの研究室メンバーが登録されると、通知設定を選べます。');return;}
    state.people.forEach(person=>{
      const item=dom('label','v5-watch');
      const input=dom('input');input.type='checkbox';input.checked=state.watches.includes(person.user_id);input.disabled=!person.arrival_sharing;
      item.append(input,dom('span','',person.display_name),dom('small','',person.arrival_sharing?'入室通知を許可中':'本人の許可待ち'));
      input.onchange=async()=>{try{await api(`/api/v5/me/watches/${encodeURIComponent(person.user_id)}`,'PUT',{enabled:input.checked});report('通知設定を保存しました');await refresh();}catch(e){report(e.message);input.checked=!input.checked;}};
      wrap.append(item);
    });
  }
  function renderToday(){
    const content=$('today-event-list');content.replaceChildren();
    const all=[...state.today_events.map(x=>({...x,type:'イベント'})),...state.today_meetings.map(x=>({...x,type:'ミーティング'}))];
    all.sort((a,b)=>a.starts_ms-b.starts_ms);
    if(!all.length)listEmpty(content,'今日のイベント・確定済みミーティングはありません。');
    all.forEach(e=>{const item=dom('article','v5-item');item.append(dom('strong','',e.title),dom('span','v5-time',`${fmt(e.starts_ms)} 〜 ${fmt(e.ends_ms)}`),dom('small','',`${e.type}${e.location?' · '+e.location:''}`));content.append(item);});
    $('events-admin').hidden=!state.admin;
  }
  function render(){
    if(!state)return;
    $('v5-sections-message').textContent='設備予約・日程調整・LINE通知を利用できます';
    renderEquipment();renderPolls();renderWatches();renderToday();
    const consent=$('presence-recommend-opt-in');consent.disabled=!state.me;
    consent.checked=!!state.preferences?.presence_recommend_opt_in;
    if (window.LabHubUI) window.LabHubUI.renderWorkspace(state);
  }
  async function refresh(){
    try {state=await api('/api/v5/overview');render();}
    catch(e){if(e.message.includes('承認済みメンバー'))return;$('v5-sections-message').textContent='利用にはLINEログインが必要です';}
  }
  async function action(url, method, payload, success){
    try{await api(url,method,payload);report(success);await refresh();return true;}
    catch(e){report(e.message);return false;}
  }
  $('equipment-form').onsubmit=ev=>{ev.preventDefault();action('/api/v5/equipment','POST',{
    name:$('eq-name').value,kind:$('eq-kind').value,location:$('eq-location').value,description:$('eq-description').value},'設備を登録しました');};
  $('book-form').onsubmit=async ev=>{
    ev.preventDefault();const eid=Number($('book-equipment').value);
    const start=ms('book-start'),end=ms('book-end');
    if(!eid)return report('先に設備を登録してください');
    if(!Number.isFinite(start)||start<Date.now()-60000)return report('利用開始は現在時刻より後に設定してください');
    if(!Number.isFinite(end)||end<=start)return report('利用終了は開始時刻より後に設定してください');
    const success=await action('/api/v5/reservations','POST',{
      equipment_id:eid,starts_ms:start,ends_ms:end,purpose:$('book-purpose').value},'設備を予約しました');
    if(success){$('v54-book-create').open=false;$('book-purpose').value='';}
  };
  $('poll-form').onsubmit=async ev=>{
    ev.preventDefault();
    if(selectedSlots.length<2)return report('候補日時を2件以上追加してください');
    const deadline=ms('poll-deadline');
    if(!Number.isFinite(deadline))return report('回答期限を入力してください');
    if(selectedSlots.some(s=>s.starts_ms<=deadline))return report('回答期限はすべての候補日時より前に設定してください');
    const success=await action('/api/v5/polls','POST',{
      title:$('poll-title').value,description:$('poll-description').value,
      deadline_ms:deadline,equipment_id:asNumber($('poll-equipment').value),
      slots:selectedSlots.slice()},'日程調整を作成しました');
    if(success){$('v54-meeting-create').open=false;selectedSlots.splice(0);renderSelectedSlots();$('poll-title').value='';$('poll-description').value='';}
  };
  $('add-slot').onclick=()=>{
    const start=ms('poll-manual-start');
    addCandidate(start,start+Number($('poll-duration').value)*60000);
  };
  $('poll-suggest').onclick=suggest;
  $('presence-recommend-opt-in').onchange=async ev=>{
    try{await api('/api/v5/me/presence-recommend-opt-in','PUT',{allow:ev.target.checked});
      report('推薦への履歴利用の許可を更新しました');await refresh();}
    catch(e){report(e.message);ev.target.checked=!ev.target.checked;}
  };
  const initial=new Date();
  const first=new Date(initial);first.setDate(first.getDate()+2);
  first.setHours(0,0,0,0);$('poll-first-day').value=dateInputValue(first);
  const deadline=new Date(initial);deadline.setDate(deadline.getDate()+1);deadline.setHours(17,0,0,0);
  $('poll-deadline').value=dateTimeInputValue(deadline);
  const manual=new Date(first);manual.setHours(14,0,0,0);
  $('poll-manual-start').value=dateTimeInputValue(manual);
  renderSelectedSlots();
  $('arrival-sharing').onchange=async ev=>{try{await api('/api/v5/me/arrival-sharing','PUT',{allow:ev.target.checked});report('入室通知の許可設定を変更しました');await refresh();}
    catch(e){report(e.message);ev.target.checked=!ev.target.checked;}};
  $('event-form').onsubmit=ev=>{ev.preventDefault();action('/api/v5/events','POST',{
    title:$('event-title').value,starts_ms:ms('event-start'),ends_ms:ms('event-end'),location:$('event-location').value},'イベントを登録しました');};
  setTimeout(refresh,1300);
  setInterval(()=>{if(!document.hidden)refresh();},20000);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
  document.addEventListener('labhub-admin-updated',refresh);
})();
