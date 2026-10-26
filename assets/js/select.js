/* ── 自定义下拉：运行时渐进增强 ────────────────────────────────────
   把页面里所有原生 <select> 换成同尺寸的自绘下拉，展开层不再由浏览器
   绘制（原生 option 列表无法用 CSS 控制，深色模式下尤其突兀）。
   原生 select 元素保留：value / selectedIndex / change 事件 / 表单提交 /
   无障碍语义全部不变，只是视觉被隐藏。JS 未执行时页面照常可用。 */
(function(){
  'use strict';
  if (window.__csEnhanced) return;
  window.__csEnhanced = true;

  var openWrap = null;

  function labelOf(sel){
    var o = sel.options[sel.selectedIndex];
    return o ? (o.textContent || o.text || '') : '';
  }

  function closeWrap(wrap){
    if (!wrap) return;
    wrap.classList.remove('cs-open');
    var trg = wrap.querySelector('.cs-trigger');
    if (trg) trg.setAttribute('aria-expanded','false');
    if (openWrap === wrap) openWrap = null;
  }

  function closeAll(except){
    if (openWrap && openWrap !== except) closeWrap(openWrap);
  }

  function syncLabel(sel, wrap){
    var v = wrap.querySelector('.cs-value');
    if (!v) return;
    var txt = labelOf(sel);
    v.textContent = txt;
    v.classList.toggle('cs-placeholder', !txt);
    var opts = wrap.querySelectorAll('.cs-opt');
    for (var i = 0; i < opts.length; i++){
      opts[i].classList.toggle('cs-selected', i === sel.selectedIndex);
      opts[i].setAttribute('aria-selected', i === sel.selectedIndex ? 'true' : 'false');
    }
  }

  function buildPanel(sel, wrap){
    var panel = wrap.querySelector('.cs-panel');
    if (!panel){
      panel = document.createElement('div');
      panel.className = 'cs-panel';
      panel.setAttribute('role','listbox');
      wrap.appendChild(panel);
    }
    panel.textContent = '';
    var n = sel.options.length;
    for (var i = 0; i < n; i++){
      var o = sel.options[i];
      var row = document.createElement('div');
      row.className = 'cs-opt' + (o.disabled ? ' cs-disabled' : '');
      row.setAttribute('role','option');
      row.setAttribute('data-idx', i);
      if (o.disabled) row.setAttribute('aria-disabled','true');
      var tick = document.createElement('span');
      tick.className = 'cs-tick';
      tick.textContent = '\u2713';
      var lab = document.createElement('span');
      lab.textContent = o.textContent || o.text || '';
      row.appendChild(tick);
      row.appendChild(lab);
      panel.appendChild(row);
    }
    if (!n){
      var em = document.createElement('div');
      em.className = 'cs-empty';
      em.textContent = '\u2014';
      panel.appendChild(em);
    }
    syncLabel(sel, wrap);
  }

  function positionPanel(wrap){
    var r = wrap.getBoundingClientRect();
    var below = window.innerHeight - r.bottom;
    var need = Math.min(320, window.innerHeight * 0.52);
    var up = below < need && r.top > below;
    wrap.classList.toggle('cs-drop-up', up);
  }

  function openWrap_(wrap){
    closeAll(wrap);
    positionPanel(wrap);
    wrap.classList.add('cs-open');
    openWrap = wrap;
    var trg = wrap.querySelector('.cs-trigger');
    if (trg) trg.setAttribute('aria-expanded','true');
    var sel = wrap.querySelector('select');
    var panel = wrap.querySelector('.cs-panel');
    if (panel && sel){
      var cur = panel.children[sel.selectedIndex];
      if (cur && cur.scrollIntoView) cur.scrollIntoView({ block: 'nearest' });
    }
  }

  function pick(sel, wrap, idx){
    var o = sel.options[idx];
    if (!o || o.disabled) return;
    if (sel.selectedIndex !== idx){
      sel.selectedIndex = idx;
      sel.dispatchEvent(new Event('change', { bubbles: true }));
    }
    syncLabel(sel, wrap);
    closeWrap(wrap);
    var trg = wrap.querySelector('.cs-trigger');
    if (trg) trg.focus();
  }

  function hoverIdx(panel, idx){
    var rows = panel.querySelectorAll('.cs-opt');
    for (var i = 0; i < rows.length; i++){
      rows[i].classList.toggle('cs-hover', i === idx);
    }
    if (rows[idx] && rows[idx].scrollIntoView) rows[idx].scrollIntoView({ block: 'nearest' });
  }

  function enhance(sel){
    if (!sel || sel.dataset.csDone === '1') return;
    if (sel.closest('.cs-wrap')) return;
    sel.dataset.csDone = '1';

    var wrap = document.createElement('span');
    wrap.className = 'cs-wrap';
    var cs = window.getComputedStyle(sel);
    if (cs.display === 'block' || cs.width === '100%' || sel.style.width === '100%'){
      wrap.classList.add('cs-block');
    }
    if (sel.id) wrap.setAttribute('data-for', sel.id);
    sel.parentNode.insertBefore(wrap, sel);
    wrap.appendChild(sel);
    sel.classList.add('cs-native');
    sel.setAttribute('tabindex','-1');

    var trg = document.createElement('button');
    trg.type = 'button';
    trg.className = 'cs-trigger';
    trg.setAttribute('aria-haspopup','listbox');
    trg.setAttribute('aria-expanded','false');
    var val = document.createElement('span');
    val.className = 'cs-value';
    var arrow = document.createElement('span');
    arrow.className = 'cs-arrow';
    trg.appendChild(val);
    trg.appendChild(arrow);
    wrap.insertBefore(trg, sel);

    buildPanel(sel, wrap);

    trg.addEventListener('click', function(e){
      e.preventDefault();
      e.stopPropagation();
      if (wrap.classList.contains('cs-open')) closeWrap(wrap);
      else openWrap_(wrap);
    });

    trg.addEventListener('keydown', function(e){
      var panel = wrap.querySelector('.cs-panel');
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp'){
        e.preventDefault();
        if (!wrap.classList.contains('cs-open')) openWrap_(wrap);
        var rows = panel.querySelectorAll('.cs-opt');
        var cur = -1;
        for (var i = 0; i < rows.length; i++) if (rows[i].classList.contains('cs-hover')) { cur = i; break; }
        var next = e.key === 'ArrowDown'
          ? Math.min(rows.length - 1, cur + 1)
          : Math.max(0, cur < 0 ? rows.length - 1 : cur - 1);
        hoverIdx(panel, next);
      } else if (e.key === 'Enter' || e.key === ' '){
        e.preventDefault();
        if (!wrap.classList.contains('cs-open')) { openWrap_(wrap); return; }
        var rows2 = panel.querySelectorAll('.cs-opt');
        for (var j = 0; j < rows2.length; j++){
          if (rows2[j].classList.contains('cs-hover')) { pick(sel, wrap, j); return; }
        }
      } else if (e.key === 'Escape'){
        closeWrap(wrap);
      } else if (e.key === 'Tab'){
        closeWrap(wrap);
      }
    });

    wrap.addEventListener('click', function(e){
      var row = e.target.closest ? e.target.closest('.cs-opt') : null;
      if (row && !row.classList.contains('cs-disabled')){
        pick(sel, wrap, parseInt(row.getAttribute('data-idx'), 10));
      }
    });

    wrap.addEventListener('mousemove', function(e){
      var row = e.target.closest ? e.target.closest('.cs-opt') : null;
      if (!row) return;
      var panel = wrap.querySelector('.cs-panel');
      var rows = panel.querySelectorAll('.cs-opt');
      for (var i = 0; i < rows.length; i++) rows[i].classList.toggle('cs-hover', rows[i] === row);
    });

    /* 外部数据改了 options（例如账号列表刷新）时重建面板 */
    if (typeof MutationObserver !== 'undefined'){
      new MutationObserver(function(){
        buildPanel(sel, wrap);
      }).observe(sel, { childList: true, subtree: true, attributes: true, attributeFilter: ['disabled','selected'] });
    }
    /* 程序直接改 value 时同步显示 */
    var desc = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value');
    if (desc && desc.set && !sel.__csPatched){
      sel.__csPatched = true;
      try {
        Object.defineProperty(sel, 'value', {
          configurable: true,
          get: function(){ return desc.get.call(sel); },
          set: function(v){ desc.set.call(sel, v); syncLabel(sel, wrap); }
        });
      } catch (err) { /* 老浏览器忽略 */ }
    }

    sel.addEventListener('change', function(){ syncLabel(sel, wrap); });
  }

  function scan(root){
    var list = (root || document).querySelectorAll('select');
    for (var i = 0; i < list.length; i++) enhance(list[i]);
  }

  document.addEventListener('click', function(e){
    if (openWrap && !openWrap.contains(e.target)) closeWrap(openWrap);
  }, true);

  document.addEventListener('keydown', function(e){
    if (e.key === 'Escape' && openWrap) closeWrap(openWrap);
  });

  window.addEventListener('resize', function(){ if (openWrap) positionPanel(openWrap); });
  /* 滚动时只重算方向，不关闭：scrollIntoView / 容器滚动都会派发 scroll，
     直接关闭会让"点开就消失"。面板相对触发器定位，跟随即可。 */
  window.addEventListener('scroll', function(){
    if (openWrap) positionPanel(openWrap);
  }, true);

  function boot(){
    scan(document);
    if (typeof MutationObserver !== 'undefined'){
      new MutationObserver(function(muts){
        for (var i = 0; i < muts.length; i++){
          var added = muts[i].addedNodes;
          for (var j = 0; j < added.length; j++){
            var n = added[j];
            if (n.nodeType !== 1) continue;
            if (n.tagName === 'SELECT') enhance(n);
            else if (n.querySelectorAll) scan(n);
          }
        }
      }).observe(document.body, { childList: true, subtree: true });
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
