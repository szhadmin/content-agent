/* 工作台前端公共脚本 —— 无框架，够用就行 */

/* 子路径部署支持：整个应用可以挂在 /wxwb 这类前缀下 */
var P = (typeof window !== 'undefined' && window.__P) ? window.__P : '';
function withPrefix(path) {
  if (!path) return P || '/';
  if (/^https?:\/\//.test(path)) return path;
  return P + path;
}

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

function toast(msg, kind = '') {
  const el = $('#toast');
  if (!el) return;
  el.textContent = msg;
  el.className = 'toast show ' + kind;
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = 'toast ' + kind; }, kind === 'err' ? 6000 : 2600);
}

async function api(path, body, method = 'POST') {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers['Content-Type'] = 'application/json';
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(withPrefix(path), opt);
  let data = {};
  try { data = await r.json(); } catch (e) { /* 空响应 */ }
  if (!r.ok || data.ok === false) {
    throw new Error(data.error || ('请求失败 ' + r.status));
  }
  return data;
}

function busy(btn, on, text) {
  if (!btn) return;
  if (on) {
    btn.dataset.label = btn.textContent;
    btn.disabled = true;
    btn.textContent = text || '处理中…';
  } else {
    btn.disabled = false;
    if (btn.dataset.label) btn.textContent = btn.dataset.label;
  }
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function fmtTime(s) {
  if (!s) return '';
  return String(s).slice(5, 16);
}

/* 把 markdown 预览注入 iframe（宽度模拟手机） */
function previewInto(iframe, html) {
  // 子路径部署时，正文里的 /media/xxx 要补上前缀，否则图片 404
  if (P) {
    html = String(html).replace(/src="\/(media|static)\//g, 'src="' + P + '/$1/');
  }
  const doc = iframe.contentDocument || iframe.contentWindow.document;
  doc.open();
  doc.write(`<!DOCTYPE html><html><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <style>body{margin:0;padding:18px 16px 40px;background:#fff;
      font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;}
      img{max-width:100%}</style></head><body>${html}</body></html>`);
  doc.close();
}
