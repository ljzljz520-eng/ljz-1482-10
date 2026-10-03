'use strict';
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const eid = new URLSearchParams(location.search).get('eid');
const userId = localStorage.getItem('wb_user') || '';

async function api(url) {
  const r = await fetch(url, { headers: { 'X-User-Id': userId } });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}
(async () => {
  try {
    const users = await api('/api/users');
    $('#userSelect').innerHTML = users.users.map(u =>
      `<option value="${u.id}" ${String(u.id) === userId ? 'selected' : ''}>${esc(u.display_name)}</option>`).join('');
    $('#userSelect').onchange = e => { localStorage.setItem('wb_user', e.target.value); location.reload(); };

    const e = await api('/api/exports/' + eid);
    const snap = e.snapshot_json;
    const groups = {};
    for (const o of e.outputs) (groups[o.channel] ||= []).push(o);
    const panels = Object.entries(groups).map(([ch, outs]) => `
      <div class="card"><h3>${esc(ch)}</h3><div class="preview-grid">
        ${outs.map(o => {
          const url = '/storage/output/' + o.path;
          if (o.kind === 'contact_sheet' || o.kind === 'preview')
            return `<div><div class="hint">${o.kind}</div><img src="${esc(url)}"></div>`;
          return `<div class="issue"><a class="out" href="${esc(url)}">下载 ${o.kind}</a></div>`;
        }).join('')}
      </div></div>`).join('');

    const issueRows = e.checks_json.issues.length
      ? e.checks_json.issues.map(i => `<div class="issue ${i.severity}"><code>${i.code}</code> ${esc(i.detail)}</div>`).join('')
      : '<div class="issue">检查全部通过</div>';

    $('#main').innerHTML = `
      <h1>导出 #${e.id} <small>制作于 ${new Date(e.created_at * 1000).toLocaleString()}</small></h1>
      <p class="hint">以下信息在制作时<strong>冻结</strong>：旧导出不自动套用当前房型描述，
        也不会出现无依据的周边推荐；快照里的规则文字与素材版本就是当时发布的真实内容。</p>
      <div class="card"><h3>发布时检查（as_of=${esc(snap.produced_as_of)}）</h3>${issueRows}</div>
      ${panels}
      <div class="card"><h3>冻结快照 JSON</h3>
        <pre style="white-space:pre-wrap;font-size:12px">${esc(JSON.stringify(snap, null, 2))}</pre></div>`;
  } catch (err) {
    $('#main').innerHTML = `<div class="issue error">${esc(err.message)}</div>`;
  }
})();
