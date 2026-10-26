(function(){
  var VARS = [
    ['--accent', '强调色', 'signal'],
    ['--accent2', '成功色', 'signal'],
    ['--warn', '警告色', 'signal'],
    ['--bad', '错误色', 'signal'],
    ['--on-accent', '主按钮字', 'onaccent'],
    ['--bg', '页面底', 'layer'],
    ['--panel', '卡片', 'layer'],
    ['--panel2', '内嵌块', 'layer'],
    ['--fg', '主文字', 'text'],
    ['--dim', '次文字', 'text'],
    ['--line', '边框', 'edge']
  ];
  var KEY = 'wb-theme-tuner';
  var styleEl = null;
  var box = document.getElementById('tunerBox');
  var rowsEl = document.getElementById('tunerRows');
  var themeLabel = document.getElementById('tunerTheme');
  var TEXT_MIN = 4.5;
  /* Kept inline: the page is served as one HTML document, and a separate
     <script src> would add a second request and a failure mode when the page
     is opened from disk. tests/_test_dashboard_theme_contrast.js executes the
     same functions from this block. */
  var COLOR = (function(){
    function clampByte(value){ return Math.max(0, Math.min(255, Math.round(Number(value)))); }
    function parseColor(value, backdrop){
      var text = String(value == null ? '' : value).trim();
      if (!text) return null;
      if (text.toLowerCase() === 'transparent') {
        var clearBase = backdrop ? parseColor(backdrop) : [255,255,255,1];
        return clearBase ? clearBase.slice() : null;
      }
      var rgba = text.match(/^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)(?:\s*,\s*([\d.]+))?\s*\)$/i);
      if (rgba){
        var alpha = rgba[4] === undefined ? 1 : Number(rgba[4]);
        if (!isFinite(alpha) || alpha < 0 || alpha > 1) return null;
        var base = backdrop ? parseColor(backdrop) : [255,255,255,1];
        if (!base) return null;
        return [
          clampByte(Number(rgba[1]) * alpha + base[0] * (1 - alpha)),
          clampByte(Number(rgba[2]) * alpha + base[1] * (1 - alpha)),
          clampByte(Number(rgba[3]) * alpha + base[2] * (1 - alpha)),
          1
        ];
      }
      var hex = text.replace(/^#/, '');
      if (hex.length === 3) hex = hex[0]+hex[0]+hex[1]+hex[1]+hex[2]+hex[2];
      if (hex.length === 4) hex = hex[0]+hex[0]+hex[1]+hex[1]+hex[2]+hex[2]+hex[3]+hex[3];
      if (/^[0-9a-f]{6}$/i.test(hex)) {
        return [parseInt(hex.slice(0,2),16), parseInt(hex.slice(2,4),16),
                parseInt(hex.slice(4,6),16), 1];
      }
      if (/^[0-9a-f]{8}$/i.test(hex)) {
        var bg = backdrop ? parseColor(backdrop) : [255,255,255,1];
        if (!bg) return null;
        var a = parseInt(hex.slice(6,8),16) / 255;
        return [
          clampByte(parseInt(hex.slice(0,2),16) * a + bg[0] * (1 - a)),
          clampByte(parseInt(hex.slice(2,4),16) * a + bg[1] * (1 - a)),
          clampByte(parseInt(hex.slice(4,6),16) * a + bg[2] * (1 - a)),
          1
        ];
      }
      return null;
    }
    function hexOf(rgb){
      if (!rgb) return null;
      return '#' + rgb.slice(0,3).map(function(v){
        return clampByte(v).toString(16).padStart(2,'0');
      }).join('');
    }
    function channel(v){ v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }
    function luminance(value){
      var rgb = parseColor(value);
      if (!rgb) return null;
      return 0.2126 * channel(rgb[0]) + 0.7152 * channel(rgb[1]) + 0.0722 * channel(rgb[2]);
    }
    function contrast(a, b){
      var la = luminance(a), lb = luminance(b);
      if (la == null || lb == null) return null;
      return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
    }
    function worstContrast(value, backgrounds){
      var worst = null;
      for (var i = 0; i < backgrounds.length; i++){
        var r = contrast(value, backgrounds[i]);
        if (r == null) return null;
        if (worst == null || r < worst) worst = r;
      }
      return worst;
    }
    function mix(from, to, amount){
      var a = parseColor(from), b = parseColor(to);
      if (!a || !b) return null;
      return [
        clampByte(a[0] + (b[0] - a[0]) * amount),
        clampByte(a[1] + (b[1] - a[1]) * amount),
        clampByte(a[2] + (b[2] - a[2]) * amount),
        1
      ];
    }
    function ensureContrast(value, backgrounds, fallback, minimum){
      var min = typeof minimum === 'number' ? minimum : TEXT_MIN;
      var ratio = worstContrast(value, backgrounds);
      if (ratio != null && ratio >= min) return { value: value, ratio: ratio, adjusted: false };
      var backup = worstContrast(fallback, backgrounds);
      if (backup != null && backup >= min) return { value: fallback, ratio: backup, adjusted: true };
      var seed = value;
      if (worstContrast(seed, backgrounds) == null) seed = fallback;
      if (worstContrast(seed, backgrounds) == null) seed = '#808080';
      var black = worstContrast('#000000', backgrounds);
      var white = worstContrast('#ffffff', backgrounds);
      var target = (white != null && (black == null || white > black)) ? '#ffffff' : '#000000';
      var best = seed, bestRatio = worstContrast(seed, backgrounds);
      for (var step = 1; step <= 255; step++){
        var candidate = hexOf(mix(seed, target, step / 255));
        var candidateRatio = worstContrast(candidate, backgrounds);
        if (candidateRatio == null) continue;
        best = candidate; bestRatio = candidateRatio;
        if (candidateRatio >= min) break;
      }
      return { value: best, ratio: bestRatio, adjusted: true };
    }
    return { parseColor: parseColor, hexOf: hexOf, contrast: contrast,
             worstContrast: worstContrast, ensureContrast: ensureContrast };
  })();

  function theme(){ return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light'; }
  /* The fallback must come from the built-in CSS, not from a saved tuner
     value: an older low-contrast custom colour must not become the new floor. */
  function readBuiltInVals(t){
    var style = document.getElementById('tunerStyle');
    var hostText = Array.prototype.map.call(document.querySelectorAll('style'), function(node){
      return node === style ? '' : (node.textContent || '');
    }).join('');
    var themeData = window.__WB_THEME_STYLES__ || {};
    var styleId = document.documentElement.getAttribute('data-theme-style') || 'claude';
    var palette = themeData[styleId] && themeData[styleId][t];
    var values = {};
    for (var i = 0; i < VARS.length; i++) {
      var name = VARS[i][0];
      var re = new RegExp((t === 'dark' ? '\\[data-theme="dark"\\]' : ':root') +
        '\\{(?=[^}]*' + name + '\\s*:)[^}]*' + name + '\\s*:\\s*([^;}]+)', 'i');
      var m = hostText.match(re);
      values[name] = (palette && palette[name]) || (m ? m[1].trim() : '');
    }
    return values;
  }
  /* 调色台的起点：当前主题风格的内置色值，统一成 #rrggbb 供取色器使用。
     不能读 getComputedStyle：:root 上的内联变量只代表当前深浅色，另一套会读错。 */
  function builtInHex(t){
    var raw = readBuiltInVals(t), out = {};
    for (var i = 0; i < VARS.length; i++){
      var name = VARS[i][0];
      var backdrop = name === '--panel2' ? raw['--panel'] : null;
      out[name] = COLOR.hexOf(COLOR.parseColor(raw[name], backdrop)) || '#000000';
    }
    return out;
  }
  function ratio(a, b){ return COLOR.contrast(a, b); }
  function check(name, kind, t){
    if (kind === 'onaccent') return ratio(t[name], t['--accent']);
    if (kind === 'text') return COLOR.worstContrast(t[name], [t['--panel'], t['--bg']]);
    if (kind === 'signal'){
      return COLOR.worstContrast(t[name], [t['--panel'], t['--bg']]);
    }
    return ratio(t[name], t['--panel']);
  }
  /* 没有自定义时调色台只展示内置色值、不写任何样式，主题风格与深浅色切换照常
     生效；改过一次颜色（或已有存档）才挂覆盖层。 */
  var edits, customized = false;
  try { var saved = JSON.parse(localStorage.getItem(KEY) || 'null'); } catch(e){ saved = null; }
  if (saved && saved.light && saved.dark && saved.light['--accent'] && saved.dark['--accent']) {
    edits = saved;
    customized = true;
  } else {
    edits = { light: builtInHex('light'), dark: builtInHex('dark') };
  }
  var DEFAULT = { light: readBuiltInVals('light'), dark: readBuiltInVals('dark') };

  /* 主题风格把色值写成 :root 上的内联变量，普通样式表规则压不过它，所以自定义
     值带 !important。看板页的颜色是全局令牌的别名，不必单独写。 */
  function apply(){
    if (!customized) return;
    if (!styleEl){ styleEl = document.createElement('style'); styleEl.id = 'tunerStyle'; document.head.appendChild(styleEl); }
    function block(t){ return Object.keys(edits[t]).map(function(k){ return k + ':' + edits[t][k] + '!important'; }).join(';'); }
    styleEl.textContent =
      ':root{' + block('light') + '}\n' +
      '[data-theme="dark"]{' + block('dark') + '}';
  }
  function save(){ try { localStorage.setItem(KEY, JSON.stringify(edits)); } catch(e){} }

  function backgrounds(name, t){
    if (name === '--on-accent') return [t['--accent']];
    if (name === '--accent' || name === '--accent2' || name === '--warn' || name === '--bad'){
      return [t['--panel'], t['--bg']];
    }
    if (name === '--fg' || name === '--dim') return [t['--panel'], t['--bg']];
    if (name === '--panel2') return [t['--panel']];
    return [t['--panel']];
  }

  function safeValue(name, kind, t, mode){
    var candidate = t[name];
    if (kind === 'layer' || kind === 'edge') return candidate;
    var fallback = (DEFAULT[mode] || {})[name] || '';
    var result = COLOR.ensureContrast(candidate, backgrounds(name, t), fallback, TEXT_MIN);
    return result.value || candidate;
  }

  function normalizeAll(){
    ['light', 'dark'].forEach(function(t){
      VARS.forEach(function(v){
        edits[t][v[0]] = safeValue(v[0], v[2], edits[t], t);
      });
    });
  }

  function renderRows(){
    var t = theme();
    themeLabel.textContent = t === 'dark' ? '暗色' : '亮色';
    rowsEl.innerHTML = '';
    VARS.forEach(function(v){
      var name = v[0], label = v[1];
      var val = edits[t][name] || '#000000';
      var row = document.createElement('div');
      row.className = 'tuner-row';
      var lab = document.createElement('label'); lab.textContent = label;
      var inp = document.createElement('input'); inp.type = 'color'; inp.value = val;
      var code = document.createElement('code'); code.textContent = val;
      var rat = document.createElement('span'); rat.className = 'ratio';
      row.appendChild(lab); row.appendChild(inp); row.appendChild(code); row.appendChild(rat);
      inp.addEventListener('input', function(){
        edits[t][name] = inp.value;
        edits[t][name] = safeValue(name, v[2], edits[t], t);
        inp.value = edits[t][name];
        code.textContent = edits[t][name];
        customized = true;
        apply(); renderRatios(); save();
      });
      rowsEl.appendChild(row);
    });
    renderRatios();
  }
  function renderRatios(){
    var t = edits[theme()];
    var panel = t['--panel'];
    var rows = rowsEl.children;
    for (var i = 0; i < VARS.length; i++){
      var name = VARS[i][0], kind = VARS[i][2];
      if(!rows || !rows[i] || !rows[i].children) continue;
      var el = rows[i].children[3];
      if(!el) continue;
      var r = check(name, kind, t);
      if (kind === 'layer'){
        if (name === '--panel') r = ratio(t['--panel'], t['--bg']);   /* 卡片行显示与页面底的层次 */
        if (name === '--panel2') r = ratio(t['--panel2'], t['--panel']);
        el.textContent = '层次 ' + (r ? r.toFixed(2) : '-'); el.className = 'ratio';
      }
      else if (kind === 'edge'){ el.textContent = '边框 ' + (r ? r.toFixed(2) : '-'); el.className = 'ratio'; }
      else {
        var ok = r != null && r >= TEXT_MIN;
        el.textContent = (r ? r.toFixed(2) : '-') + (ok ? ' ✓' : ' ⚠');
        el.className = 'ratio ' + (ok ? 'ok' : 'low');
      }
    }
  }

  /* 主题菜单入口：打开/关闭面板，并同步菜单项的选中态 */
  window.toggleThemeTuner = function(evt){
    if (evt){ evt.preventDefault(); evt.stopPropagation(); }
    var menu = document.getElementById('themeDropdown');
    if (menu) menu.classList.remove('open');
    var btn = document.getElementById('themeToggleBtn');
    if (btn) btn.setAttribute('aria-expanded', 'false');
    var open = box.classList.toggle('open');
    var opt = document.getElementById('themeOptTuner');
    if (opt) opt.classList.toggle('active', open);
  };

  /* 拖动 */
  document.getElementById('tunerHead').addEventListener('mousedown', function(e){
    if (e.target.id === 'tunerClose') return;
    var r = box.getBoundingClientRect();
    var ox = e.clientX - r.left, oy = e.clientY - r.top;
    function mv(ev){
      box.style.left = Math.max(0, ev.clientX - ox) + 'px';
      box.style.top = Math.max(0, ev.clientY - oy) + 'px';
      box.style.right = 'auto';
    }
    function up(){ document.removeEventListener('mousemove', mv); document.removeEventListener('mouseup', up); }
    document.addEventListener('mousemove', mv);
    document.addEventListener('mouseup', up);
    e.preventDefault();
  });
  document.getElementById('tunerClose').addEventListener('click', function(){
    box.classList.remove('open');
    var opt = document.getElementById('themeOptTuner');
    if (opt) opt.classList.remove('active');
  });
  document.getElementById('tunerReset').addEventListener('click', function(){
    try { localStorage.removeItem(KEY); } catch(e){}
    if (styleEl){ styleEl.remove(); styleEl = null; }
    customized = false;
    edits = { light: builtInHex('light'), dark: builtInHex('dark') };
    renderRows();
  });
  document.getElementById('tunerExport').addEventListener('click', function(){
    var txt = JSON.stringify(edits, null, 2);
    function fallback(){ window.prompt('复制以下调色结果：', txt); }
    if (navigator.clipboard && navigator.clipboard.writeText){
      navigator.clipboard.writeText(txt).then(function(){ alert('已复制到剪贴板 ✓'); }, fallback);
    } else { fallback(); }
  });
  /* 切深浅色或主题风格时调色台跟着切；没自定义过就换成新风格的内置色值 */
  if (typeof MutationObserver !== 'undefined') {
    new MutationObserver(function(){
      DEFAULT = { light: readBuiltInVals('light'), dark: readBuiltInVals('dark') };
      if (!customized) edits = { light: builtInHex('light'), dark: builtInHex('dark') };
      renderRows();
    }).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'data-theme-style'] });
  }

  normalizeAll();
  apply();
  renderRows();
})();
