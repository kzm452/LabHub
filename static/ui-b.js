/* LabHub B UI adapter. Data only from the existing authorized v5.1 endpoints. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const NAV = ['home','members','booking','schedule'];
  function homeGreeting(){
    const hour=Number(new Intl.DateTimeFormat('en-GB',{timeZone:'Asia/Tokyo',hour:'2-digit',hourCycle:'h23'}).format(new Date()));
    return hour>=5&&hour<11?'おはようございます ☀':hour>=11&&hour<18?'こんにちは ☀':'こんばんは ☾';
  }
  const LABELS = {members:'研究室のメンバー',booking:'設備予約',schedule:'研究室の予定'};
  function text(tag, cls, content) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    el.textContent = content;
    return el;
  }
  function initials(name) {return [...(name || '?')].slice(0, 2).join('');}
  function navigate(target, updateHash=true) {
    if (!NAV.includes(target)) target='home';
    for (const item of NAV) {
      const panel=document.querySelector(`[data-lab-screen="${item}"]`);
      panel.hidden = item !== target;
      panel.classList.toggle('is-active', item===target);
    }
    document.querySelectorAll('[data-lab-nav]').forEach(button => {
      const selected = button.dataset.labNav===target;
      button.classList.toggle('is-active',selected);
      if(selected) button.setAttribute('aria-current','page');
      else button.removeAttribute('aria-current');
    });
    const heading = $('lab-page-title');
    heading.textContent = target==='home' ? homeGreeting() : LABELS[target];
    if (updateHash && location.hash.slice(1)!==target) history.replaceState(null,'','#'+target);
    window.scrollTo({top:0,behavior:'auto'});
  }
  document.querySelectorAll('[data-lab-nav], [data-lab-go]').forEach(button => button.addEventListener('click',()=>navigate(button.dataset.labNav||button.dataset.labGo)));
  document.querySelector('[data-brand-home]').addEventListener('click',event=>{event.preventDefault();navigate('home');});
  window.addEventListener('hashchange',()=>navigate(location.hash.slice(1),false));
  navigate(location.hash.slice(1),false);
  // Existing manager button scrolls to add-member form, so switch screens before using it.
  $('admin-open').addEventListener('click',()=>navigate('members'));

  function renderPresence(data) {
    if (!data || !data.approved) return;
    const members = Array.isArray(data.members) ? data.members : [];
    const people=members.filter(m=>m.status==='present'||m.status==='auto');
    const count = Number(data.member_count)||0;
    const present = Number(data.present_count)||0;
    const pct = count ? Math.min(100,Math.max(0,Math.round(present/count*100))) : 0;
    $('lab-progress-fill').style.width = pct + '%';
    $('lab-progress-fill').parentElement.setAttribute('aria-valuenow',String(pct));
    $('lab-present-count-note').textContent = `${people.length} MEMBERS`;
    $('lab-all-members-label').textContent=people.length>3 ? `残り${people.length-3}人を含む全員を見る` : 'メンバー一覧を見る';
    const own = members.find(m=>m.mine);
    const status=$('lab-my-position');
    if (own && status) {
      const label=own.status==='auto'?'在室 · 自動検知':own.status==='present'?'在室':own.status==='away'?'不在':'不明';
      status.textContent='● '+label;
      status.className='lab-my-position is-'+own.status;
    }
    const name = own?.name;
    if(name){$('lab-user-name').textContent=name;$('lab-user-avatar').textContent=initials(name);}
    const target=$('lab-present-preview');target.replaceChildren();
    if (!people.length){target.append(text('p','lab-preview-empty','現在、在室と判定されたメンバーはいません。'));return;}
    for(const member of people.slice(0,3)) {
      const card=text('div','lab-preview-person','');
      card.append(text('span','lab-person-avatar',initials(member.name)),
                  text('strong','lab-person-name',member.name),
                  text('span','lab-person-activity',member.activity && member.activity!=='未設定'?member.activity:'活動は未設定'));
      card.title=`${member.name}：${member.activity||'未設定'}`;
      target.appendChild(card);
    }
  }
  function fmt(ms){
    if (!Number.isFinite(Number(ms))) return '';
    return new Intl.DateTimeFormat('ja-JP',{timeZone:'Asia/Tokyo',hour:'2-digit',minute:'2-digit',month:'numeric',day:'numeric'}).format(new Date(Number(ms)));
  }
  function addEntry(target,icon,title,detail) {
    const row=text('div','lab-home-entry','');
    row.append(text('span','lab-entry-icon',icon));
    const info=text('div','lab-entry-text','');
    info.append(text('strong','',title),text('small','',detail));
    row.append(info);target.append(row);
  }
  function renderWorkspace(data){
    if (!data || !data.me && !data.admin) return; // authorized v5 overview only
    const events=$('lab-home-events');events.replaceChildren();
    const today=[...(data.today_events||[]).map(e=>({...e,kind:'イベント'})),...(data.today_meetings||[]).map(e=>({...e,kind:'ミーティング'}))];
    today.sort((a,b)=>a.starts_ms-b.starts_ms);
    if(!today.length)events.append(text('p','lab-hint','今日の予定はありません。'));
    today.slice(0,3).forEach(e=>addEntry(events,'▤',e.title,`${fmt(e.starts_ms)} · ${e.kind}${e.location?' · '+e.location:''}`));
    const bookings=$('lab-home-bookings');bookings.replaceChildren();
    const active=(data.reservations||[]).filter(e=>e.status!=='cancelled' && e.ends_ms>Date.now()).sort((a,b)=>a.starts_ms-b.starts_ms);
    if(!active.length)bookings.append(text('p','lab-hint','現在、近日中の予約はありません。'));
    active.slice(0,3).forEach(e=>addEntry(bookings,'▦',e.equipment_name,`${fmt(e.starts_ms)} 〜 ${fmt(e.ends_ms)}`));
  }
  window.LabHubUI={navigate,renderPresence,renderWorkspace};
})();
