(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const state = { data: null, filter: 'all', key: sessionStorage.getItem('labhub_admin_key') || '', idToken: '' };
  const activities = ['未設定', '研究中', '食事中', '授業中', '外出中', '会議中', '帰宅', '対応可能', '集中中', '離席中', '会話OK'];
  let toastTimer;

  function toast(message) {
    const el = $('toast');
    el.textContent = message;
    el.classList.add('is-visible');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('is-visible'), 3600);
  }

  async function api(path, opts = {}) {
    const headers = { ...(opts.headers || {}) };
    if (state.key) headers['X-LabHub-Key'] = state.key;
    if (state.idToken) headers['Authorization'] = `Bearer ${state.idToken}`;
    if (opts.body) headers['Content-Type'] = 'application/json';
    const response = await fetch(path, { ...opts, headers, cache: 'no-store' });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 401 && path.startsWith('/api/admin/')) {
        state.key = '';
        sessionStorage.removeItem('labhub_admin_key');
      }
      throw new Error(payload.error || (response.status === 503 ? '管理者キーがサーバーに設定されていません' : `通信エラー (${response.status})`));
    }
    return payload;
  }

  function element(tag, className = '', text = '') {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text) el.textContent = text;
    return el;
  }

  function initials(name) {
    if (!name) return '?';
    return [...name].slice(0, 2).join('');
  }

  const presenceLabels = {
    present: '● 在室',
    auto: '◉ 在室（自動検知）',
    away: '○ 不在',
    unknown: '？ 不明'
  };

  function memberRow(member, admin) {
    const row = element('article', 'member-row');
    const present = member.status === 'present' || member.status === 'auto';
    const avatar = element('div', 'member-avatar' + (present ? '' : ' is-away'), initials(member.name));
    const content = element('div', 'member-content');
    content.appendChild(element('div', 'member-name', member.name));
    const details = [];
    if (member.last_enter) details.push(`最終入室 ${member.last_enter}`);
    if (member.last_auto) details.push(`最終自動検知 ${member.last_auto}`);
    if (member.activity !== '未設定') details.push(`活動：${member.activity}`);
    content.appendChild(element('div', 'member-sub', details.join(' · ')));
    row.append(avatar, content);
    row.appendChild(element('span', `member-status is-${member.status}`, presenceLabels[member.status] || '？ 不明'));
    if (member.activity !== '未設定') {
      row.appendChild(element('span', 'member-activity-tag', member.activity));
    }
    if (!admin && member.mine && state.idToken) {
      const actions = element('div', 'member-actions');
      const toggle = element('button', 'mini-button' + (present ? ' exit' : ''), present ? '退室する' : '入室する');
      toggle.type = 'button';
      toggle.addEventListener('click', () => changeOwnPresence(present ? 'away' : 'present', toggle));
      actions.appendChild(toggle);
      row.appendChild(actions);
    }
    if (admin && member.id) {
      const actions = element('div', 'member-actions');
      const toggle = element('button', 'mini-button' + (present ? ' exit' : ''), present ? '退室にする' : '入室にする');
      toggle.type = 'button';
      toggle.addEventListener('click', () => changePresence(member.id, present ? 'away' : 'present', toggle));
      const select = element('select', 'activity-select');
      select.setAttribute('aria-label', `${member.name} のコミュニケーション状態`);
      for (const activity of activities) {
        const option = element('option', '', activity);
        option.value = activity;
        option.selected = member.activity === activity;
        select.appendChild(option);
      }
      select.addEventListener('change', () => changeActivity(member.id, select.value));
      actions.append(toggle, select);
      row.appendChild(actions);
    }
    return row;
  }

  function render() {
    const d = state.data;
    if (!d) return;
    $('auth-notice').hidden = !!d.approved;
    $('protected-dashboard').hidden = !d.approved;
    if (!d.approved) {
      $('self-presence-panel').hidden = true;
      $('self-activity-panel').hidden = true;
      $('auth-title').textContent = d.authenticated ? '研究室管理者の承認待ちです' : 'LINEログインが必要です';
      $('auth-description').textContent = d.authenticated
        ? 'LINEログインは完了しました。管理者が承認すると在室メンバーの名前を確認できます。'
        : '研究室の在室情報は承認済みメンバーにのみ公開されます。';
      $('line-login').hidden = !!d.authenticated || !window.liff;
      $('sync-label').textContent = '認証待ち';
      return;
    }
    $('present-count').textContent = String(d.present_count);
    $('member-count').textContent = String(d.member_count);
    $('presence-rate').textContent = d.member_count ? String(Math.round(d.present_count / d.member_count * 100)) : '0';
    $('member-count-label').textContent = `${d.member_count} members`;
    $('presence-note').textContent = `「在室（自動検知）」はLINE BeaconまたはLabHub専用iBeaconからの推定です。記録から${d.presence_hours}時間経つと「不明」に変わります（自動退室ではありません）。`;
    $('sync-label').textContent = '最新データ';
    $('admin-status').textContent = d.can_edit ? '管理者モード · 手動操作が有効' : (d.admin_enabled ? '現在は閲覧モードです' : '閲覧モード · 管理者キー未設定');
    $('admin-open').firstChild.textContent = d.can_edit ? 'メンバーを追加する ' : '管理者として操作する ';
    const time = new Date(d.updated_at);
    $('today-label').textContent = `${new Intl.DateTimeFormat('ja-JP', { timeZone: 'Asia/Tokyo', year: 'numeric', month: 'long', day: 'numeric', weekday: 'long' }).format(time)} の在室状況`;

    const visible = d.members.filter(m => state.filter === 'all' ||
      (state.filter === 'present' && (m.status === 'present' || m.status === 'auto')) || m.status === state.filter);
    const selfPanel = $('self-presence-panel');
    const selfMember = d.self_member;
    selfPanel.hidden = !d.can_edit_self || !selfMember;
    $('self-activity-panel').hidden = !d.can_edit_self || !selfMember;
    if (selfMember) {
      renderOwnActivity(selfMember);
      $('self-presence-status').textContent = presenceLabels[selfMember.status] || '？ 不明';
      $('self-checkin').disabled = selfMember.status === 'present';
      $('self-checkout').disabled = selfMember.status === 'away';
    }

    const list = $('member-list');
    list.replaceChildren();
    if (!visible.length) {
      const empty = element('div', 'empty-state');
      empty.appendChild(element('span', 'empty-icon', '◌'));
      empty.appendChild(element('strong', '', d.members.length ? 'この条件に該当するメンバーはいません' : 'まだメンバーが登録されていません'));
      empty.appendChild(element('p', '', d.members.length ? '別のフィルターを選んでください。' : '新しいビーコンイベントを受信するか、管理者モードでメンバーを追加してください。'));
      list.appendChild(empty);
    } else {
      visible.forEach(m => list.appendChild(memberRow(m, d.can_edit)));
    }
    if (d.can_edit && d.pending && d.pending.length) {
      const pending = element('section', 'pending-members');
      pending.appendChild(element('h3', '', `承認待ちのLINEユーザー（${d.pending.length}人）`));
      d.pending.forEach(m => {
        const item = element('div', 'pending-member');
        item.appendChild(element('span', '', m.name));
        const approve = element('button', 'mini-button', '研究室メンバーとして承認');
        approve.type = 'button';
        approve.addEventListener('click', async () => {
          approve.disabled = true;
          try { await api(`/api/admin/members/${encodeURIComponent(m.id)}/approve`, { method: 'POST' }); toast('承認しました'); await refresh(); }
          catch (e) { toast(e.message); approve.disabled = false; }
        });
        item.appendChild(approve);
        pending.appendChild(item);
      });
      list.appendChild(pending);
    }
    if (d.can_edit) {
      const form = element('form', 'add-member');
      const name = element('input');
      name.type = 'text';
      name.required = true;
      name.maxLength = 40;
      name.placeholder = '新しいメンバーの名前';
      name.setAttribute('aria-label', '新しいメンバーの名前');
      const add = element('button', '', '＋ 追加');
      add.type = 'submit';
      form.append(name, add);
      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        if (!name.value.trim()) return;
        add.disabled = true;
        try {
          await api('/api/admin/members', { method: 'POST', body: JSON.stringify({ name: name.value.trim() }) });
          toast('メンバーを追加しました');
          await refresh();
        } catch (error) { toast(error.message); add.disabled = false; }
      });
      list.appendChild(form);
    }
  }

  async function refresh() {
    try {
      state.data = await api('/api/presence');
      render();
      if (window.LabHubUI) window.LabHubUI.renderPresence(state.data);
    } catch (error) {
      $('sync-label').textContent = '接続エラー';
      toast('サーバーに接続できません: ' + error.message);
    }
  }

  function jaTime(ms) {
    return new Intl.DateTimeFormat('ja-JP', { timeZone: 'Asia/Tokyo',
      month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(ms));
  }

  function renderOwnActivity(selfMember) {
    const current = selfMember.activity || '未設定';
    $('self-activity-current').textContent = current;
    $('self-activity-expiry').textContent = current === '未設定' ? '現在は未設定です'
      : (selfMember.activity_expires_ms ? `${jaTime(selfMember.activity_expires_ms)} に自動解除` : '手動解除まで継続');
    document.querySelectorAll('#activity-options [data-activity]').forEach(button => {
      const selected = button.dataset.activity === current;
      button.classList.toggle('is-selected', selected);
      button.setAttribute('aria-pressed', String(selected));
    });
  }

  async function updateOwnActivity(activity, button) {
    const ttl = activity === '未設定' ? 0 : Number($('activity-ttl').value);
    if (button) button.disabled = true;
    try {
      await api('/api/me/activity', { method: 'POST', body: JSON.stringify({activity, ttl_minutes: ttl}) });
      toast(activity === '未設定' ? '活動状態を解除しました' : `活動状態を「${activity}」に変更しました`);
      await refresh();
      await loadActivityHistory();
    } catch (error) {
      toast(error.message);
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function loadActivityHistory() {
    if (!state.idToken) return;
    try {
      const data = await api('/api/me/activity/history');
      const target = $('activity-history');
      target.replaceChildren();
      if (!data.history.length) {
        target.appendChild(element('p', 'activity-history-empty', '変更履歴はまだありません。'));
        return;
      }
      for (const entry of data.history) {
        const suffix = entry.expires_ms ? `（${jaTime(entry.expires_ms)}まで）` : '';
        target.appendChild(element('div', 'activity-history-line', `${jaTime(entry.changed_ms)} · ${entry.activity}${suffix}`));
      }
    } catch (error) {
      $('activity-history').textContent = '履歴を取得できませんでした';
    }
  }

  async function changeOwnPresence(status, button) {
    button.disabled = true;
    try {
      await api('/api/me/presence', { method: 'POST', body: JSON.stringify({ status }) });
      toast(status === 'present' ? '入室を記録しました' : '退室を記録しました');
      await refresh();
    } catch (error) { toast(error.message); button.disabled = false; }
  }

  async function changePresence(id, status, button) {
    button.disabled = true;
    try {
      await api(`/api/admin/members/${encodeURIComponent(id)}/presence`, { method: 'POST', body: JSON.stringify({ status }) });
      toast(status === 'present' ? '入室に変更しました' : '退室に変更しました');
      await refresh();
    } catch (error) { toast(error.message); button.disabled = false; }
  }

  async function changeActivity(id, activity) {
    try {
      await api(`/api/admin/members/${encodeURIComponent(id)}/activity`, { method: 'POST', body: JSON.stringify({ activity, ttl_minutes: 60 }) });
      toast('状態を更新しました');
      await refresh();
    } catch (error) { toast(error.message); await refresh(); }
  }

  document.querySelectorAll('#activity-options [data-activity]').forEach(button => {
    button.addEventListener('click', () => updateOwnActivity(button.dataset.activity, button));
  });
  $('activity-reset').addEventListener('click', () => updateOwnActivity('未設定', $('activity-reset')));
  document.querySelector('.activity-history-details').addEventListener('toggle', (event) => {
    if (event.currentTarget.open) loadActivityHistory();
  });
  $('self-checkin').addEventListener('click', () => changeOwnPresence('present', $('self-checkin')));
  $('self-checkout').addEventListener('click', () => changeOwnPresence('away', $('self-checkout')));

  const dialog = $('admin-dialog');
  function openAdmin() {
    if (state.data?.can_edit) {
      const form = $('member-list').querySelector('.add-member');
      window.LabHubUI?.navigate('members');
      form?.scrollIntoView({ behavior: 'smooth', block: 'center' });
      form?.querySelector('input')?.focus();
      return;
    }
    if (!state.data?.admin_enabled) {
      toast('サーバー起動前に LABHUB_ADMIN_KEY を設定してください');
      return;
    }
    $('dialog-error').textContent = '';
    dialog.showModal();
    $('admin-key').focus();
  }
  $('admin-open').addEventListener('click', openAdmin);
  $('auth-admin-open').addEventListener('click', openAdmin);
  $('mobile-admin').addEventListener('click', openAdmin);
  $('close-dialog').addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); });
  $('admin-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const entered = $('admin-key').value.trim();
    if (!entered) return;
    state.key = entered;
    try {
      await api('/api/admin/check', { method: 'POST' });
      sessionStorage.setItem('labhub_admin_key', entered);
      $('admin-key').value = '';
      dialog.close();
      toast('管理者モードでログインしました');
      await refresh();
    } catch (_) {
      $('dialog-error').textContent = '管理者キーが違います。もう一度確認してください。';
    }
  });
  document.querySelectorAll('.filter-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      state.filter = tab.dataset.filter;
      document.querySelectorAll('.filter-tab').forEach(other => {
        other.classList.toggle('is-active', other === tab);
        other.setAttribute('aria-selected', String(other === tab));
      });
      render();
    });
  });
  document.querySelectorAll('[data-coming-soon]').forEach(button => {
    button.addEventListener('click', () => toast(`${button.dataset.comingSoon}は今後追加予定です`));
  });
  $('line-login').addEventListener('click', () => {
    if (window.liff && !liff.isLoggedIn()) liff.login({ redirectUri: location.href });
  });
  async function initialize() {
    if (window.liff && window.LABHUB_LIFF_ID) {
      try {
        await liff.init({ liffId: window.LABHUB_LIFF_ID });
        if (liff.isLoggedIn()) {
          state.idToken = liff.getIDToken() || '';
          if (state.idToken) await api('/api/auth/me', { method: 'POST' });
        }
      } catch (err) { toast('LINE認証の初期化を確認してください: ' + err.message); }
    }
    await refresh();
  }
  initialize();
  setInterval(refresh, 15000);
})();
