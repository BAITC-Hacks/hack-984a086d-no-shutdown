// Wind Dispatch — интерактивный фронтенд на JavaScript.
// Мок-данные: genForecast(). Отрисовка: render() и draw().
(() => {
  const root = document.getElementById('wind-dispatch');
  const q = s => root.querySelector(s);
  const fmt = (n, digits=1) => n.toLocaleString('ru-RU', {minimumFractionDigits:digits, maximumFractionDigits:digits});
  const dt = t => new Date(t).toLocaleString('ru-RU', {timeZone:'UTC', day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
  const validDate = d => typeof d === 'string' && /^2026-02-(0[1-9]|1\d|2[0-8])$/.test(d);
  const turbines = {T1:{capacity:2.5, offset:0},T2:{capacity:3.2, offset:1.8}};
  let state = {turbine:'T1',date:'2026-02-10',horizon:48,revision:0,hour:12,power:true,wind:true};
  let data, busy=false, request=0;
  function restore(saved) {
    const s=saved?.modelContent;
    if (!s) return;
    if (turbines[s.turbine]) state.turbine=s.turbine;
    if (validDate(s.date)) state.date=s.date;
    state.horizon=s.horizon===24?24:48;
    state.revision=Number.isInteger(s.revision)?Math.max(0,Math.min(s.revision,100000)):0;
    const p=saved.privateContent||{};
    state.hour=Number.isInteger(p.hour)?Math.max(0,Math.min(p.hour,state.horizon-1)):12;
    state.power=p.power!==false;state.wind=p.wind!==false;
  }
  // Восстанавливаем выбор из браузера; сервер для хранения не нужен.
  try { restore(JSON.parse(localStorage.getItem('wind-dispatch-state'))); } catch {}
  function save() {
    try { localStorage.setItem('wind-dispatch-state', JSON.stringify({modelContent:{turbine:state.turbine,date:state.date,horizon:state.horizon,revision:state.revision,dataMode:'mock'},privateContent:{hour:state.hour,power:state.power,wind:state.wind}})); } catch {}
  }
  // API-compatible response. predicted_power is mean MW over [timestamp, timestamp + 1h).
  // Forecast weather is synthetic; it is not observed future weather.
  function genForecast(turbineId, asOfDate, revision=0) {
    const turbine=turbines[turbineId], day=Number(asOfDate.slice(-2));
    let seed=(day*3919+(turbineId==='T1'?101:709)+revision*1103)>>>0;
    const random=()=>{seed=(Math.imul(seed,1664525)+1013904223)>>>0;return seed/4294967296;};
    const start=Date.parse(asOfDate+'T00:00:00Z');
    const forecast=Array.from({length:48},(_,h)=>{
      const wind=Math.max(2.5,Math.min(16,8.2+turbine.offset*.55+2.4*Math.sin(h/6+day*.37+turbine.offset)+1.35*Math.sin(h/2.8+day*.16)+(random()-.5)*.8+Math.sin(revision*.65)*.28));
      const temperature=-5+day*.11+3.1*Math.sin((h%24-8)*Math.PI/12)+(random()-.5)*.7;
      const power=turbine.capacity*Math.max(0,Math.min(1,(wind**3-3**3)/(12**3-3**3)))*(.95+random()*.05);
      return {timestamp:new Date(start+h*3600000).toISOString(),predicted_power:+power.toFixed(3),wind_speed:+wind.toFixed(2),temperature:+temperature.toFixed(2)};
    });
    const max=Math.max(...forecast.map(f=>f.wind_speed));
    const warnings=[{severity:'warning',title:'Горизонт +36–48 ч',message:'Демо-сигнал: дальний прогноз требует проверки неопределённости.'},max>11.5?{severity:'warning',title:'Усиление ветра',message:'В мок-прогнозе ветер достигает '+fmt(max)+' м/с. Следите за изменением мощности.'}:{severity:'info',title:'Погодный ряд заполнен',message:'48 из 48 часовых интервалов. Пропусков в мок-данных нет.'}];
    return {turbine_id:turbineId,as_of_date:asOfDate,generated_at:new Date().toISOString(),horizon_hours:48,forecast,warnings:warnings.map(w=>w.title+': '+w.message),warning_details:warnings};
  }
  function metric(key,value) {q('[data-metric="'+key+'"]').textContent=value;}
  function render() {
    const f=data.forecast, sum=n=>f.slice(0,n).reduce((s,r)=>s+r.predicted_power,0);
    const mean=k=>f.reduce((s,r)=>s+r[k],0)/f.length;
    const peak=f.reduce((a,b)=>b.predicted_power>a.predicted_power?b:a);
    metric('energy24',fmt(sum(24)));metric('energy48',fmt(sum(48)));
    metric('capacity','КИУМ за 48 ч · '+fmt(sum(48)/(48*turbines[state.turbine].capacity)*100,0)+' %');
    metric('peak',fmt(peak.predicted_power,2));metric('peakTime',dt(peak.timestamp)+' UTC');
    metric('wind',fmt(mean('wind_speed')));metric('weatherWind',fmt(mean('wind_speed')));
    metric('windRange','Диапазон '+fmt(Math.min(...f.map(x=>x.wind_speed)))+'–'+fmt(Math.max(...f.map(x=>x.wind_speed)))+' м/с');
    metric('temperature',fmt(mean('temperature')));metric('maxWind',fmt(Math.max(...f.map(x=>x.wind_speed))));
    q('[name=turbine]').value=state.turbine;q('[name=date]').value=state.date;
    q('[data-cutoff]').textContent='Срез данных: '+dt(state.date+'T00:00:00Z')+' UTC';
    q('[data-metadata]').textContent='as_of_date = '+data.as_of_date+' · generated_at = '+data.generated_at+' · mock run #'+(state.revision+1);
    q('.wd-status').textContent=state.turbine+' · '+dt(f[0].timestamp)+' → '+dt(Date.parse(f[0].timestamp)+48*3600000)+' UTC';
    q('[data-warning-count]').textContent=data.warnings.length+' · демо';
    q('.wd-warnings').replaceChildren(...data.warning_details.map(w=>{
      const row=document.createElement('div');row.className='wd-warning '+(w.severity==='info'?'info':'');
      const icon=document.createElement('i');icon.dataset.lucide=w.severity==='info'?'info':'triangle-alert';icon.setAttribute('aria-hidden','true');
      const body=document.createElement('div'),title=document.createElement('strong'),message=document.createElement('p');title.textContent=w.title;message.textContent=w.message;body.append(title,message);row.append(icon,body);return row;
    }));
    globalThis.lucide?.createIcons({attrs:{width:17,height:17}});
    root.querySelectorAll('[data-horizon]').forEach(b=>b.setAttribute('aria-pressed',Number(b.dataset.horizon)===state.horizon));
    root.querySelectorAll('[data-series]').forEach(b=>b.setAttribute('aria-pressed',state[b.dataset.series]));
    q('#wd-hour').max=state.horizon-1;q('#wd-hour').value=state.hour;
    detail();draw();
  }
  function detail(){
    const r=data.forecast[state.hour];q('[data-hour-label]').textContent='+'+state.hour+' ч';
    q('.wd-detail').textContent=dt(r.timestamp)+' UTC'+(state.power?' · '+fmt(r.predicted_power,2)+' МВт':'')+(state.wind?' · '+fmt(r.wind_speed)+' м/с':'');
  }
  function draw(){
    if(!globalThis.d3){q('.wd-status').textContent='Не удалось загрузить график. Сводка и переключатели доступны.';return;}
    const host=q('.wd-plot'),width=host.clientWidth;if(!width)return;
    const height=294, m={top:29,right:43,bottom:43,left:46},inner=width-m.left-m.right;
    const x=d3.scaleLinear().domain([0,state.horizon]).range([m.left,width-m.right]);
    const y=d3.scaleLinear().domain([0,turbines[state.turbine].capacity*1.08]).nice().range([height-m.bottom,m.top]);
    const windMax=d3.max(data.forecast,d=>d.wind_speed)*1.15;
    const yw=d3.scaleLinear().domain([0,windMax]).nice().range([height-m.bottom,m.top]);
    const svg=d3.select(q('.wd-plot svg')).attr('viewBox',`0 0 ${width} ${height}`).attr('height',height);
    const oldPower=q('.wd-power-line')?.getAttribute('d'),oldWind=q('.wd-wind-line')?.getAttribute('d');svg.selectAll('*').interrupt();svg.selectAll('*').remove();
    svg.append('title').text('Прогноз '+state.turbine+' на '+state.horizon+' часов. Мощность в МВт, ветер в м/с.');
    svg.append('desc').text('Показаны синтетические почасовые средние; значения доступны через ползунок под графиком.');
    const defs=svg.append('defs');const gradient=defs.append('linearGradient').attr('id','wd-area').attr('x1','0').attr('y1','0').attr('x2','0').attr('y2','1');
    gradient.append('stop').attr('offset','0%').attr('stop-color','var(--gold)').attr('stop-opacity',.19);gradient.append('stop').attr('offset','100%').attr('stop-color','var(--gold)').attr('stop-opacity',.01);
    if(state.horizon===48)svg.append('rect').attr('x',x(24)).attr('y',m.top).attr('width',x(48)-x(24)).attr('height',height-m.bottom-m.top).attr('fill','#ffffff').attr('opacity',.018);
    svg.append('rect').attr('data-chart-frame','').attr('x',m.left).attr('y',m.top).attr('width',inner).attr('height',height-m.bottom-m.top).attr('fill','none').attr('stroke','var(--edge)');
    const yt=y.ticks(4);yt.forEach(t=>{
      svg.append('line').attr('x1',m.left).attr('x2',width-m.right).attr('y1',y(t)).attr('y2',y(t)).attr('stroke','var(--edge)').attr('stroke-width',.7);
      svg.append('text').attr('x',m.left-9).attr('y',y(t)+4).attr('text-anchor','end').text(fmt(t));
    });
    yw.ticks(4).forEach(t=>svg.append('text').attr('x',width-m.right+9).attr('y',yw(t)+4).text(fmt(t,0)));
    const ticks=width<420?[0,state.horizon/2,state.horizon]:d3.range(0,state.horizon+1,state.horizon/4);
    ticks.forEach(t=>svg.append('text').attr('x',x(t)).attr('y',height-m.bottom+22).attr('text-anchor',t===0?'start':t===state.horizon?'end':'middle').text('+'+t+' ч'));
    svg.append('text').attr('class','axis-title').attr('data-axis','y').attr('x',m.left).attr('y',14).text('МВт');
    svg.append('text').attr('x',width-m.right).attr('y',14).attr('text-anchor','end').text('м/с');
    svg.append('text').attr('class','axis-title').attr('data-axis','x').attr('x',width/2).attr('y',height-3).attr('text-anchor','middle').text('Часы от среза · UTC');
    const rows=data.forecast.slice(0,state.horizon).map((r,i)=>({...r,h:i}));rows.push({...rows[rows.length-1],h:state.horizon});
    // Step curve: every observation represents a full hour. 48 rows cover exactly 48h.
    const line=(key,scale)=>d3.line().x(r=>x(r.h)).y(r=>scale(r[key])).curve(d3.curveStepAfter)(rows);
    const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
    function path(cls,d,color,previous,dash){const p=svg.append('path').attr('class',cls).attr('fill','none').attr('stroke',color).attr('stroke-width',2.2).attr('stroke-linejoin','round').attr('d',d);if(dash)p.attr('stroke-dasharray',dash);if(previous&&!reduced)p.attr('d',previous).transition().duration(280).attr('d',d);}
    if(state.power){svg.append('path').attr('d',d3.area().x(r=>x(r.h)).y0(y(0)).y1(r=>y(r.predicted_power)).curve(d3.curveStepAfter)(rows)).attr('fill','url(#wd-area)');path('wd-power-line',line('predicted_power',y),'var(--gold)',oldPower);}
    if(state.wind)path('wd-wind-line',line('wind_speed',yw),'var(--teal)',oldWind,'5 4');
    if(state.horizon===48){svg.append('line').attr('x1',x(24)).attr('x2',x(24)).attr('y1',m.top).attr('y2',height-m.bottom).attr('stroke','var(--sub)').attr('stroke-dasharray','3 5');svg.append('text').attr('x',x(24)).attr('y',15).attr('text-anchor','middle').text('+24 ч');}
    const guide=svg.append('line').attr('data-chart-hover-guide','').attr('y1',m.top).attr('y2',height-m.bottom).attr('stroke','var(--sub)').attr('opacity',.55).attr('pointer-events','none');
    const markers=[];if(state.power)markers.push({key:'predicted_power',scale:y,color:'var(--gold)'});if(state.wind)markers.push({key:'wind_speed',scale:yw,color:'var(--teal)'});
    markers.forEach(o=>o.node=svg.append('circle').attr('data-chart-hover-marker','').attr('r',4).attr('fill',o.color).attr('stroke','var(--panel)').attr('stroke-width',2).attr('pointer-events','none'));
    function position(hour,cursorX){const r=data.forecast[hour];guide.attr('x1',cursorX).attr('x2',cursorX);markers.forEach(o=>o.node.attr('cx',cursorX).attr('cy',o.scale(r[o.key])));}
    position(state.hour,x(state.hour));
    const tip=q('.wd-tooltip');tip.hidden=true;
    svg.append('rect').attr('data-chart-hit','').attr('data-chart-hover-overlay','cross-series').attr('x',m.left).attr('y',m.top).attr('width',inner).attr('height',height-m.bottom-m.top).attr('fill','transparent')
    .on('pointermove',function(event){const [px]=d3.pointer(event,this),hour=Math.max(0,Math.min(state.horizon-1,Math.floor(x.invert(px)))),r=data.forecast[hour];position(hour,px);tip.innerHTML='<span class="wd-mono">'+dt(r.timestamp)+' UTC</span>'+(state.power?'<div><span>Мощность</span><span class="wd-mono">'+fmt(r.predicted_power,2)+' МВт</span></div>':'')+(state.wind?'<div><span>Ветер</span><span class="wd-mono">'+fmt(r.wind_speed)+' м/с</span></div>':'');tip.hidden=false;tip.style.left=Math.max(0,Math.min(px+14,width-tip.offsetWidth))+'px';tip.style.top='36px';})
    .on('pointerleave',()=>{tip.hidden=true;position(state.hour,x(state.hour));})
    .on('click',function(event){const [px]=d3.pointer(event,this);state.hour=Math.max(0,Math.min(state.horizon-1,Math.floor(x.invert(px))));q('#wd-hour').value=state.hour;detail();save();position(state.hour,x(state.hour));});
  }
  function changeSelection(){
    if(!validDate(q('[name=date]').value)){q('.wd-error').hidden=false;q('.wd-error').textContent='Выберите дату с 1 по 28 февраля 2026 года. Показан последний корректный прогноз.';return false;}
    q('.wd-error').hidden=true;
    const turbine=q('[name=turbine]').value,date=q('[name=date]').value;
    if(turbine!==state.turbine||date!==state.date){state.turbine=turbine;state.date=date;state.revision=0;data=genForecast(turbine,date,0);render();save();}return true;
  }
  q('[name=turbine]').addEventListener('change',changeSelection);q('[name=date]').addEventListener('change',changeSelection);
  q('.wd-refresh').addEventListener('click',async e=>{e.preventDefault();if(busy||!changeSelection())return;busy=true;const token=++request;const button=q('.wd-refresh');button.disabled=true;q('[name=turbine]').disabled=true;q('[name=date]').disabled=true;button.querySelector('span').textContent='Расчёт…';q('.wd-status').textContent='Обновление синтетической погоды → пересчёт мощности…';await new Promise(resolve=>setTimeout(resolve,650));if(token===request){state.revision++;data=genForecast(state.turbine,state.date,state.revision);render();save();}busy=false;button.disabled=false;q('[name=turbine]').disabled=false;q('[name=date]').disabled=false;button.querySelector('span').textContent='Пересчитать';});
  root.querySelectorAll('[data-horizon]').forEach(b=>b.addEventListener('click',()=>{state.horizon=Number(b.dataset.horizon);state.hour=Math.min(state.hour,state.horizon-1);render();save();}));
  root.querySelectorAll('[data-series]').forEach(b=>b.addEventListener('click',()=>{const key=b.dataset.series;state[key]=!state[key];render();save();}));
  q('#wd-hour').addEventListener('input',e=>{state.hour=Number(e.target.value);detail();draw();});q('#wd-hour').addEventListener('change',save);
  data=genForecast(state.turbine,state.date,state.revision);render();
  new ResizeObserver(()=>draw()).observe(q('.wd-plot'));
})();
