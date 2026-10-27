/* RightTrend chart. KLineChart 9.8.9 is vendored under Apache-2.0; see vendor/. */
(() => {
  'use strict';
  const el = id => document.getElementById(id);
  const root = el('market-chart-card');
  if (!root) return;
  const arr = value => Array.isArray(value) ? value : [];
  const number = value => value !== null && value !== undefined && Number.isFinite(Number(value));
  const timestamp = value => {
    if (value === null || value === undefined || value === '') return null;
    const n = typeof value === 'number' || /^\d+$/.test(String(value)) ? Number(value) : Date.parse(value);
    return Number.isFinite(n) ? n : null;
  };
  const date = value => timestamp(value) === null ? '待确认' : new Date(timestamp(value)).toLocaleString('zh-CN', {hour12:false});
  const price = value => number(value) ? Number(value).toLocaleString('zh-CN', {maximumFractionDigits:Number(value) >= 100 ? 2 : 6}) : '—';
  const line = color => ({style:'solid', smooth:false, size:1, color, dashedValue:[2, 2]});
  const colors = {up:'#45bd91', down:'#ed7b79', muted:'#7f948b', grid:'#20312a', text:'#c5d6cd'};
  const emaPeriods = [5, 10, 20, 40, 60, 80];
  const emaColors = ['#e5ece8','#f38a9d','#7eb8f3','#edb16d','#b89de6','#6fc7bf'];
  const state = {inst:'ETH-USDT-SWAP', level:'4H', indicator:'macd', data:null, chartKey:null,
    requestId:0, controller:null, loading:false, initialized:false, momentumPane:null, seriesByTimestamp:new Map(),
    layers:{ema:true, bi:true, segments:false, centers:true, signals:true, levels:false}};
  let chart = null;

  function showError(message) {
    el('chart-error').textContent = message;
    el('chart-error').hidden = !message;
  }
  if (!window.klinecharts) {
    el('chart-loading').textContent = '图表组件加载失败';
    showError('请刷新页面重试。');
    window.RightTrendChart = {setInstruments() {}};
    return;
  }

  klinecharts.registerOverlay({name:'rtLine', totalStep:3, lock:true,
    createPointFigures:({coordinates, overlay}) => coordinates.length < 2 ? [] : [{
      type:'line', attrs:{coordinates}, styles:{color:overlay.extendData?.color || '#e5bc58',
        size:overlay.extendData?.size || 1.5, style:overlay.extendData?.dashed ? 'dashed' : 'solid', dashedValue:[5,4]}, ignoreEvent:true,
    }],
  });
  klinecharts.registerOverlay({name:'rtCenter', totalStep:3, lock:true,
    createPointFigures:({coordinates, overlay}) => {
      if (coordinates.length < 2) return [];
      const [a, b] = coordinates, locked = overlay.extendData?.locked === true;
      return [{type:'rect', attrs:{x:Math.min(a.x,b.x), y:Math.min(a.y,b.y), width:Math.abs(a.x-b.x), height:Math.abs(a.y-b.y)},
        styles:{style:'stroke_fill', color:locked ? 'rgba(91,158,225,.12)' : 'rgba(91,158,225,.05)', borderColor:locked ? '#649bd0' : '#52728e', borderSize:1, borderStyle:locked ? 'solid' : 'dashed', borderDashedValue:[5,4]}, ignoreEvent:true}];
    },
  });
  klinecharts.registerOverlay({name:'rtMark', totalStep:2, lock:true,
    createPointFigures:({coordinates, overlay}) => {
      if (!coordinates[0]) return [];
      const c = coordinates[0], d = overlay.extendData || {}, above = d.above;
      return [{type:'text', attrs:{x:c.x, y:c.y+(above ? -12 : 12), text:d.text || '', align:'center', baseline:above ? 'bottom' : 'top'},
        styles:{color:d.color || '#fff', size:10, family:'sans-serif', weight:'normal', backgroundColor:d.bg || '#2f7058', borderRadius:3, paddingLeft:4, paddingRight:4, paddingTop:2, paddingBottom:2}, ignoreEvent:true}];
    },
  });
  klinecharts.registerOverlay({name:'rtLevel', totalStep:2, lock:true,
    createPointFigures:({coordinates, bounding, overlay}) => {
      if (!coordinates[0]) return [];
      const c = coordinates[0], d = overlay.extendData || {};
      return [{type:'line', attrs:{coordinates:[{x:0,y:c.y},{x:bounding.width,y:c.y}]},
        styles:{color:d.color || '#6a9182', size:1, style:'dashed', dashedValue:[5,5]}, ignoreEvent:true},
      {type:'text', attrs:{x:bounding.width-6, y:c.y-3, text:d.text || '', align:'right', baseline:'bottom'},
        styles:{color:d.color || '#9ab4a7', size:10, family:'sans-serif', backgroundColor:'#111a18dd', paddingLeft:3, paddingRight:3, paddingTop:1, paddingBottom:1}, ignoreEvent:true}];
    },
  });

  // Read series from the API: chart values use exactly the backend's smoothing and warmup.
  const register = (name, shortName, params, figures, field, styles, extra={}) => klinecharts.registerIndicator({
    name, shortName, calcParams:params, figures, precision:2, styles, ...extra,
    calc:data => data.map(row => state.seriesByTimestamp.get(row.timestamp)?.[field] || row.rt?.[field] || {}),
  });
  register('RT_EMA','EMA',emaPeriods,emaPeriods.map(n=>({key:'ema'+n,title:`EMA${n}: `,type:'line'})), 'ema', {lines:emaColors.map(line)}, {series:'price'});
  register('RT_MACD','MACD',[10,20,5],[
    {key:'dif',title:'DIF: ',type:'line'},{key:'dea',title:'DEA: ',type:'line'},
    {key:'hist',title:'柱: ',type:'bar',baseValue:0,styles:({current})=>{
      const color = (current.indicatorData?.hist || 0) >= 0 ? colors.up : colors.down;
      return {style:'fill',color,borderColor:color};
    }},
  ],'macd',{lines:[line('#e5c678'),line('#8eafea')]});
  register('RT_KDJ','KDJ',[9,3,3],['k','d','j'].map(key=>({key,title:key.toUpperCase()+': ',type:'line'})), 'kdj',{lines:[line('#e5c678'),line('#8eafea'),line('#c59de5')]});
  register('RT_RSI','RSI',[6,12,24],[6,12,24].map(n=>({key:'rsi'+n,title:`RSI${n}: `,type:'line'})), 'rsi',{lines:[line('#e5c678'),line('#8eafea'),line('#c59de5')]}, {minValue:0,maxValue:100});

  function initChart() {
    chart = klinecharts.init('market-chart');
    if (!chart) throw new Error('图表容器初始化失败');
    chart.setLocale('zh-CN');
    chart.setTimezone(Intl.DateTimeFormat().resolvedOptions().timeZone);
    chart.setStyles({
      grid:{horizontal:{color:colors.grid}, vertical:{color:colors.grid}},
      candle:{bar:{upColor:colors.up, downColor:colors.down, noChangeColor:colors.muted,
        upBorderColor:colors.up, downBorderColor:colors.down, noChangeBorderColor:colors.muted,
        upWickColor:colors.up, downWickColor:colors.down, noChangeWickColor:colors.muted},
        priceMark:{high:{color:colors.muted}, low:{color:colors.muted}, last:{upColor:colors.up, downColor:colors.down, noChangeColor:colors.muted}},
        tooltip:{text:{color:colors.text, size:11}}},
      indicator:{ohlc:{upColor:colors.up, downColor:colors.down},
        bars:[{style:'fill',borderStyle:'solid',borderSize:1,borderDashedValue:[2,2],upColor:colors.up,downColor:colors.down,noChangeColor:colors.muted}],
        tooltip:{text:{color:colors.text,size:10}}},
      xAxis:{axisLine:{color:colors.grid},tickText:{color:colors.muted,size:10},tickLine:{color:colors.grid}},
      yAxis:{axisLine:{color:colors.grid},tickText:{color:colors.muted,size:10},tickLine:{color:colors.grid}},
      separator:{color:colors.grid},
      crosshair:{horizontal:{line:{color:'#668777'},text:{backgroundColor:'#395346'}},vertical:{line:{color:'#668777'},text:{backgroundColor:'#395346'}}},
    });
    chart.createIndicator('RT_EMA',true,{id:'candle_pane'});
    chart.createIndicator({name:'VOL',calcParams:[20]},false,{height:75});
    state.momentumPane = chart.createIndicator('RT_MACD',false,{height:110});
    chart.setOffsetRightDistance(55);
    resetViewport();
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(el('market-chart'));
  }
  function resetViewport() {
    if (!chart) return;
    chart.setBarSpace(Math.max(3, (el('market-chart').clientWidth - 100) / 140));
    chart.scrollToRealTime();
  }
  function cutoff() { return timestamp(state.data?.analysis_as_of) || Date.now(); }
  function confirmed(item) {
    const known = timestamp(item?.known_at), locked = timestamp(item?.locked_at);
    return item?.locked === true && known !== null && locked !== null && known <= cutoff() && locked <= cutoff();
  }
  function signalStatus(ev) {
    if (ev?.invalidated) return '已失效';
    if (!confirmed(ev)) return '未确认';
    return ev?.active === false ? '历史已确认' : '已确认';
  }
  function drawOverlays() {
    if (!chart || !state.data) return;
    chart.removeOverlay({groupId:'rt-structure'});
    const data = state.data, ch = data.chan || {}, end = arr(data.candles).at(-1)?.ts;
    const put = o => chart.createOverlay({...o,groupId:'rt-structure',lock:true});
    const strokes = (items,color,size) => arr(items).filter(b=>b.state !== 'blocked' && number(b.start_ts) && number(b.end_ts) && number(b.start_price) && number(b.end_price)).forEach(b=>put({
      name:'rtLine',points:[{timestamp:b.start_ts,value:b.start_price},{timestamp:b.end_ts,value:b.end_price}],
      extendData:{color:b.unfinished ? '#80958b' : color,size,dashed:!confirmed(b)},
    }));
    if (state.layers.centers) arr(ch.zhongshu_display || ch.zhongshu).forEach(z=>{
      if (![z.start_ts,z.end_ts,z.zg,z.zd].every(number)) return;
      put({name:'rtCenter',points:[{timestamp:z.start_ts,value:z.zg},{timestamp:z.end_ts,value:z.zd}],extendData:{locked:confirmed(z)}});
    });
    if (state.layers.bi) strokes(ch.bi,'#e5bc58',1.5);
    if (state.layers.segments) strokes(ch.xianduan,'#b69be4',2.4);
    if (state.layers.signals) {
      const evidence = new Map(arr(data.evidence).filter(e=>e.kind==='bsp').map(e=>[`${e.anchor_ts}|${e.type}`,e]));
      arr(ch.bsp).forEach(s=>{
        if (!number(s.ts) || !number(s.price)) return;
        const ev = evidence.get(`${s.ts}|${s.type}`) || s, status = signalStatus(ev), buy = String(s.type).startsWith('B');
        const faded = status === '已失效' || ev.active === false;
        put({name:'rtMark',points:[{timestamp:s.ts,value:s.price}],extendData:{
          text:`${s.type} ${status}`,above:!buy,color:faded ? '#b7c5bd' : '#fff',
          bg:faded ? '#39463f' : confirmed(ev) ? (buy ? '#267654' : '#9b514d') : (buy ? '#315a49' : '#694942'),
        }});
      });
    }
    if (state.layers.levels && end) arr(data.key_levels).filter(k=>/-ZG$|-ZD$|-RECENT_HIGH$|-RECENT_LOW$/.test(k.id || '')).forEach(k=>{
      if (!number(k.price)) return;
      put({name:'rtLevel',points:[{timestamp:end,value:k.price}],extendData:{text:`${k.name} ${price(k.price)}`,color:'#9ab4a7'}});
    });
  }

  function renderSignalInfo() {
    const box = el('chart-signals');
    box.replaceChildren();
    const recent = arr(state.data?.evidence).filter(e=>e.kind==='bsp').sort((a,b)=>(b.anchor_ts || 0)-(a.anchor_ts || 0)).slice(0,3);
    recent.forEach(ev=>{
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'rt-chart-signal';
      const title = document.createElement('strong');
      title.textContent = `${ev.type} · ${signalStatus(ev)}`;
      button.append(title, document.createTextNode(`${price(ev.price)} · ${confirmed(ev) ? '确认于 '+date(ev.locked_at) : '等待结构锁定'}`));
      button.title = `极值时间：${date(ev.anchor_ts)}\n最早可知：${date(ev.known_at)}\n锁定确认：${date(ev.locked_at)}${ev.invalidated ? '\n失效原因：'+ev.invalidated : ''}${ev.active === false ? '\n已离开当前活跃信号范围' : ''}`;
      button.onclick = () => chart.scrollToTimestamp(Number(ev.anchor_ts));
      box.appendChild(button);
    });
  }
  function syncControls() {
    el('chart-instrument').value = state.inst;
    el('market-chart-title').textContent = `${state.inst.split('-')[0]} 行情分析图`;
    el('market-chart').setAttribute('aria-label',`${state.inst} ${state.level} K线、缠论结构、成交量和${state.indicator.toUpperCase()}交互图`);
    el('chart-periods').querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.level===state.level)));
    el('chart-indicators').querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.indicator===state.indicator)));
  }
  function mapRows(data) {
    const inds = data.indicators || {};
    return arr(data.candles).map((c,i)=>{
      const rt = {};
      ['ema','macd','kdj','rsi'].forEach(key=>{
        rt[key] = {};
        Object.entries(inds[key] || {}).forEach(([name,series])=>{
          if (number(series?.[i])) rt[key][name] = Number(series[i]);
        });
      });
      return {timestamp:Number(c.ts),open:Number(c.o),high:Number(c.h),low:Number(c.l),close:Number(c.c),volume:Number(c.vol),rt};
    });
  }
  function canAppendRows(previous, next) {
    // updateData can replace only the last candle or append newer candles. Older
    // corrections, filled gaps and a rolling server window need reconciliation.
    if (!previous.length || next.length < previous.length) return false;
    const fields = ['open','high','low','close','volume'];
    return previous.every((row,index) => {
      const incoming = next[index];
      return incoming?.timestamp === row.timestamp &&
        (index === previous.length-1 || fields.every(field => incoming[field] === row[field]));
    });
  }
  function captureViewport(previous) {
    if (!previous.length) return null;
    const range = chart.getVisibleRange();
    const index = Math.max(0,Math.min(previous.length-1,Math.floor((range.from+range.to)/2)));
    const anchor = previous[index].timestamp;
    const point = chart.convertToPixel({timestamp:anchor},{paneId:'candle_pane'});
    return Number.isFinite(point?.x) ? {anchor,x:point.x,barSpace:chart.getBarSpace()} : null;
  }
  function restoreViewport(viewport, rows) {
    if (!viewport || !rows.length) return;
    // A dropped anchor can no longer be displayed; use its closest surviving
    // candle. Otherwise keep the same timestamp at the exact same screen x.
    const anchor = rows.reduce((best,row) => Math.abs(row.timestamp-viewport.anchor) < Math.abs(best.timestamp-viewport.anchor) ? row : best,rows[0]);
    chart.setBarSpace(viewport.barSpace);
    chart.scrollToTimestamp(anchor.timestamp);
    const point = chart.convertToPixel({timestamp:anchor.timestamp},{paneId:'candle_pane'});
    if (Number.isFinite(point?.x)) chart.scrollByDistance(viewport.x-point.x);
  }
  async function applyData(data, requestId) {
    const rows = mapRows(data);
    if (!rows.length) throw new Error('暂时没有可显示的已收盘K线');
    const key = `${data.inst_id}|${data.level}`, previous = chart.getDataList(), prev = previous.at(-1);
    const sameKey = key === state.chartKey;
    const canIncrement = sameKey && canAppendRows(previous,rows);
    const viewport = sameKey && !canIncrement ? captureViewport(previous) : null;
    state.seriesByTimestamp = new Map(rows.map(row=>[row.timestamp,row.rt]));
    const p = rows.at(-1).close;
    chart.setPriceVolumePrecision(p >= 100 ? 2 : p >= 1 ? 4 : 6,2);
    // KLineChart updates asynchronously; wait until series are applied before overlays.
    if (canIncrement) {
      for (const row of rows.filter(r=>r.timestamp>=prev.timestamp)) {
        if (requestId !== state.requestId) return;
        await new Promise(resolve=>chart.updateData(row,resolve));
      }
    } else {
      await new Promise(resolve=>chart.applyNewData(rows,false,resolve));
      if (requestId !== state.requestId) return;
      if (sameKey) restoreViewport(viewport,rows);
      else resetViewport();
    }
    if (requestId !== state.requestId) return;
    state.data = data;
    state.chartKey = key;
    drawOverlays();
    renderSignalInfo();
    el('chart-price').textContent = price(p);
    el('chart-price-note').textContent = `${state.level} 最近已收盘价格`;
    const quality = data.quality || {};
    const qWarnings = [...arr(quality.issues),...arr(quality.warnings)].map(v=>typeof v==='string' ? v : JSON.stringify(v));
    const qualityNote = quality.stale || quality.status==='stale' ? ' · 数据已过期' : quality.has_gaps || Number(quality.gap_count)>0 || Number(quality.gaps)>0 ? ' · 存在K线缺口' : quality.status && quality.status!=='ok' ? ' · 数据状态 '+quality.status : '';
    const dayMode = {utc8:'UTC+8',utc:'UTC'}[data.daily_bar_mode] || data.daily_bar_mode || '—';
    el('chart-status').textContent = `${data.inst_id} · ${data.level} · ${rows.length} 根已收盘K线 · 收盘至 ${date(quality.last_closed_at || data.analysis_as_of)} · 更新 ${date(data.fetched_at || data.analysis_as_of)}${qualityNote} · 日周线 ${dayMode}${qWarnings.length ? ' · '+qWarnings.join('；') : ''}`;
    if (data.forming_bar && number(data.forming_bar.c)) el('chart-price-note').textContent += ` · 未收盘参考 ${price(data.forming_bar.c)}`;
  }
  async function load({switching=false}={}) {
    const id = ++state.requestId;
    state.controller?.abort();
    const controller = new AbortController();
    state.controller = controller;
    state.loading = true;
    const requestedInst = state.inst, requestedLevel = state.level;
    syncControls(); showError('');
    el('chart-refresh').disabled = true;
    el('chart-loading').hidden = false;
    el('chart-loading').dataset.refresh = String(Boolean(state.data) && !switching);
    el('chart-loading').textContent = state.data && !switching ? '正在更新…' : `正在加载 ${state.inst.split('-')[0]} · ${state.level}…`;
    if (switching) {
      state.data = null; state.chartKey = null;
      state.seriesByTimestamp = new Map();
      chart.removeOverlay({groupId:'rt-structure'}); chart.clearData();
      el('chart-price').textContent = '—'; el('chart-price-note').textContent = '最近已收盘价格';
      el('chart-signals').replaceChildren();
      el('chart-status').textContent = '正在获取所选产品与周期…';
    }
    const timeout = setTimeout(()=>controller.abort(),55000);
    try {
      const response = await fetch(`/api/chart?inst=${encodeURIComponent(requestedInst)}&level=${encodeURIComponent(requestedLevel)}`,{signal:controller.signal});
      const data = await response.json();
      if (id !== state.requestId) return;
      if (!response.ok || data.code !== 0) throw new Error(data.message || data.msg || data.error || `请求失败 (${response.status})`);
      if (data.inst_id !== requestedInst || data.level !== requestedLevel) throw new Error('行情响应与所选产品或周期不一致，请重试');
      await applyData(data,id);
    } catch (error) {
      if (id !== state.requestId) return;
      showError(`${error.name === 'AbortError' ? '行情请求超时' : '图表加载失败：'+error.message}。点击“刷新图表”重试。${state.data ? '当前保留上次成功加载的数据。' : ''}`);
      if (!state.data) el('chart-status').textContent = '行情暂不可用';
    } finally {
      clearTimeout(timeout);
      if (id === state.requestId) {
        state.loading = false; el('chart-refresh').disabled = false; el('chart-loading').hidden = true;
      }
    }
  }

  try { initChart(); } catch (error) {
    el('chart-loading').textContent = '图表初始化失败'; showError(error.message);
    window.RightTrendChart = {setInstruments() {}}; return;
  }
  el('chart-instrument').onchange = event => {
    if (state.inst === event.target.value) return;
    state.inst = event.target.value; load({switching:true});
  };
  el('chart-periods').onclick = event => {
    const button = event.target.closest('button[data-level]');
    if (!button || button.dataset.level === state.level) return;
    state.level = button.dataset.level; load({switching:true});
  };
  el('chart-refresh').onclick = () => load();
  el('chart-reset').onclick = resetViewport;
  el('chart-options').onchange = event => {
    const layer = event.target.dataset.layer;
    if (!(layer in state.layers)) return;
    state.layers[layer] = event.target.checked;
    if (layer === 'ema') {
      if (state.layers.ema) chart.createIndicator('RT_EMA',true,{id:'candle_pane'});
      else chart.removeIndicator('candle_pane','RT_EMA');
    } else drawOverlays();
  };
  el('chart-indicators').onclick = event => {
    const button = event.target.closest('button[data-indicator]');
    if (!button || button.dataset.indicator === state.indicator) return;
    const oldName = 'RT_'+state.indicator.toUpperCase();
    state.indicator = button.dataset.indicator;
    chart.removeIndicator(state.momentumPane,oldName);
    state.momentumPane = chart.createIndicator('RT_'+state.indicator.toUpperCase(),false,{height:110});
    syncControls();
  };
  window.RightTrendChart = {setInstruments(instruments) {
    const values = [...new Set(arr(instruments).filter(v=>typeof v === 'string' && v))];
    if (!values.length) values.push('ETH-USDT-SWAP');
    const old = state.inst;
    if (!values.includes(state.inst)) state.inst = values.includes('ETH-USDT-SWAP') ? 'ETH-USDT-SWAP' : values[0];
    const selector = el('chart-instrument');
    if (Array.from(selector.options,option=>option.value).join('|') !== values.join('|')) {
      selector.replaceChildren(...values.map(value=>{const option=document.createElement('option');option.value=value;option.textContent=value;return option;}));
    }
    syncControls();
    if (!state.initialized || old !== state.inst) { state.initialized = true; load({switching:old!==state.inst}); }
  }};
  // Keep the chart usable even if the independent status/report request fails.
  setTimeout(()=>{if (!state.initialized) {state.initialized=true;load();}},1500);
  setInterval(()=>{if (!document.hidden && el('chart-auto').checked && !state.loading) load();},60000);
})();
