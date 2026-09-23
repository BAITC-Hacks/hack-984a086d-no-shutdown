// Live dashboard. Forecast data comes only from the same-origin live API.
(() => {
  const root = document.getElementById('wind-dispatch');
  const q = selector => root.querySelector(selector);
  const fmt = (value, digits = 1) => Number(value).toLocaleString('ru-RU', {minimumFractionDigits: digits, maximumFractionDigits: digits});
  const utc = value => new Date(value).toLocaleString('ru-RU', {timeZone: 'UTC', day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'});
  const utcFull = value => new Date(value).toLocaleString('ru-RU', {timeZone: 'UTC', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'});
  const number = value => Number.isFinite(Number(value)) ? Number(value) : 0;
  const viewHorizon = () => Math.min(state.horizon, number(data?.horizon_hours) || state.horizon);
  let state = {turbine: 'T1', horizon: 48, hour: 12, power: true, wind: true};
  let data = null, controller = null, sequence = 0, lastSuccess = null, statusBase = 'Получение первого прогноза…';

  try {
    const saved = JSON.parse(localStorage.getItem('wind-dispatch-state'));
    if (saved?.turbine === 'T1' || saved?.turbine === 'T2') state.turbine = saved.turbine;
    if (saved?.horizon === 24 || saved?.horizon === 48) state.horizon = saved.horizon;
    if (Number.isInteger(saved?.hour)) state.hour = Math.max(0, Math.min(saved.hour, state.horizon - 1));
    state.power = saved?.power !== false;
    state.wind = saved?.wind !== false;
  } catch {}
  function save() {
    try { localStorage.setItem('wind-dispatch-state', JSON.stringify(state)); } catch {}
  }
  function turbineKey(item) {
    const id = String(item?.turbine_id ?? '');
    return id === '1' || id.toUpperCase() === 'T1' ? 'T1' : id === '2' || id.toUpperCase() === 'T2' ? 'T2' : null;
  }
  function selected() { return data?.turbines?.find(item => turbineKey(item) === state.turbine); }
  function validate(payload) {
    if (!payload || payload.status !== 'ok' || !Array.isArray(payload.turbines)) throw new Error('Сервер вернул неполный прогноз.');
    for (const id of ['T1', 'T2']) {
      const item = payload.turbines.find(row => turbineKey(row) === id);
      if (!item || !Array.isArray(item.points) || item.points.length < payload.horizon_hours) throw new Error(`В ответе отсутствует полный ряд ${id}.`);
    }
  }
  function setStatus(text, mode = 'ready') {
    statusBase = text;
    q('.wd-status').textContent = text;
    q('.wd-tag').dataset.state = mode;
    q('.wd-tag').textContent = mode === 'stale' ? 'УСТАРЕВШИЕ ДАННЫЕ' : mode === 'loading' ? 'ЗАГРУЗКА ПРОГНОЗА' : mode === 'error' ? 'API НЕДОСТУПЕН' : 'LIVE · ПРОГНОЗ МОДЕЛИ';
  }
  function showError(message, stale) {
    const panel = q('.wd-error');
    panel.hidden = false;
    panel.textContent = stale && lastSuccess
      ? `${message} Показан последний успешный прогноз от ${utc(lastSuccess)} UTC на ${data.horizon_hours} ч; данные помечены как устаревшие.`
      : `${message} Проверьте, что локальный API запущен, и повторите запрос.`;
  }
  async function loadForecast({refresh = false} = {}) {
    if (controller) controller.abort();
    controller = new AbortController();
    const ownController = controller, token = ++sequence;
    q('.wd-error').hidden = true;
    q('.wd-refresh').disabled = true;
    q('.wd-refresh span').textContent = 'Загрузка…';
    setStatus(data ? 'Проверка погодных данных и обновление расчёта…' : 'Получение первого прогноза…', 'loading');
    try {
      const params = new URLSearchParams({hours: String(state.horizon), refresh: String(refresh)});
      const response = await fetch(`/api/live?${params}`, {signal: ownController.signal, headers: {'Accept': 'application/json'}});
      if (!response.ok) {
        let detail = `Live API вернул HTTP ${response.status}.`;
        try { const body = await response.json(); if (body.detail) detail = body.detail; } catch {}
        throw new Error(detail);
      }
      const payload = await response.json();
      validate(payload);
      if (token !== sequence) return;
      data = payload;
      lastSuccess = payload.issued_at || new Date().toISOString();
      state.hour = Math.min(state.hour, Math.max(0, state.horizon - 1));
      q('.wd-error').hidden = true;
      setStatus(`Прогноз выдан ${utcFull(payload.issued_at)} UTC · начало ${utcFull(payload.forecast_start)} UTC · ${payload.horizon_hours} часов`, 'ready');
      render();
      save();
    } catch (error) {
      if (error.name === 'AbortError' || token !== sequence) return;
      const message = error.message || 'Не удалось получить прогноз.';
      showError(message, Boolean(data));
      setStatus(data ? `Устарело · последняя успешная выдача ${utc(lastSuccess)} UTC` : 'Прогноз не загружен', data ? 'stale' : 'error');
    } finally {
      if (token === sequence) {
        q('.wd-refresh').disabled = false;
        q('.wd-refresh span').textContent = 'Обновить';
      }
    }
  }
  function metric(key, value) { const node = q(`[data-metric="${key}"]`); if (node) node.textContent = value; }
  function renderWarnings() {
    const warnings = data?.warnings || [];
    const count = q('[data-warning-count]');
    count.textContent = `${warnings.length} · live`;
    const details = [...(data?.warning_details || [])];
    if (data?.model?.age_warning) details.push({severity: 'warning', title: 'Модель обучена давно', message: `Последние данные обучения: ${utcFull(data.model.trained_at)} UTC, возраст ${data.model.age_days} дней. Учитывайте ограниченную актуальность модели.`});
    for (const limitation of data?.limitations || []) details.push({severity: 'info', title: 'Ограничение прогноза', message: limitation});
    for (const warning of warnings) if (!details.some(item => item.message === warning)) details.push({severity: 'info', title: 'Источник данных', message: warning});
    count.textContent = `${details.length} · live`;
    q('.wd-warnings').replaceChildren(...details.map(warning => {
      const row = document.createElement('div'); row.className = `wd-warning ${warning.severity === 'info' ? 'info' : ''}`;
      const icon = document.createElement('i'); icon.dataset.lucide = warning.severity === 'info' ? 'info' : 'triangle-alert'; icon.setAttribute('aria-hidden', 'true');
      const body = document.createElement('div'), title = document.createElement('strong'), message = document.createElement('p');
      title.textContent = warning.title; message.textContent = warning.message; body.append(title, message); row.append(icon, body); return row;
    }));
    if (!details.length && warnings.length) q('.wd-warnings').replaceChildren(...warnings.map(message => { const p = document.createElement('p'); p.className = 'wd-warning'; p.textContent = message; return p; }));
    globalThis.lucide?.createIcons({attrs: {width: 17, height: 17}});
  }
  function render() {
    if (!data) return;
    const turbine = selected();
    if (!turbine) return;
    const horizon = viewHorizon(), points = turbine.points.slice(0, horizon), current = turbine.current || {}, capacity = number(data.capacity?.per_turbine_mw);
    const energy24 = points.slice(0, 24).reduce((sum, row) => sum + number(row.energy_mwh), 0);
    const energyAll = points.reduce((sum, row) => sum + number(row.energy_mwh), 0);
    const farmPoints = data.farm?.points?.slice(0, horizon) || [];
    const farmEnergy24 = farmPoints.slice(0, 24).reduce((sum, row) => sum + number(row.energy_mwh), 0);
    const farmEnergyAll = farmPoints.reduce((sum, row) => sum + number(row.energy_mwh), 0);
    const meanWind = points.reduce((sum, row) => sum + number(row.wind_speed), 0) / Math.max(1, points.length);
    const meanTemp = points.reduce((sum, row) => sum + number(row.temperature), 0) / Math.max(1, points.length);
    const peak = points.reduce((best, row) => number(row.power_mw) > number(best.power_mw) ? row : best, points[0]);
    const maxWind = Math.max(...points.map(row => number(row.wind_speed)));
    metric('energy24', fmt(energy24)); metric('energy48', fmt(energyAll));
    metric('capacity', `Номинал ${fmt(capacity, 1)} МВт · пользовательское значение`);
    q('[data-horizon-label]').textContent = `Энергия / выбранный горизонт (${horizon} ч)`;
    q('[data-peak-label]').textContent = `Пиковая мощность / ${horizon} ч`;
    q('[data-wind-label]').textContent = `Средний ветер / ${horizon} ч`;
    q('[data-farm-energy24]').textContent = fmt(farmEnergy24);
    q('[data-farm-energy-horizon]').textContent = fmt(farmEnergyAll);
    q('[data-farm-horizon-label]').firstChild.textContent = `Энергия / ${horizon} ч: `;
    metric('peak', fmt(peak?.power_mw, 2)); metric('peakTime', `${utc(peak?.timestamp)} UTC`);
    metric('wind', fmt(meanWind)); metric('windRange', `Среднее за ${horizon} ч`);
    metric('weatherWind', fmt(current.wind_speed_100m)); metric('temperature', fmt(current.temperature_2m)); metric('maxWind', fmt(maxWind));
    q('[name=turbine]').value = state.turbine;
    q('[data-issued]').textContent = `Выпущен: ${utcFull(data.issued_at)} UTC · начало: ${utcFull(data.forecast_start)} UTC · горизонт ${data.horizon_hours} ч`;
    q('[data-metadata]').textContent = `Модель: ${data.model?.name || '—'} · обучена: ${data.model?.trained_at ? utcFull(data.model.trained_at) + ' UTC' : '—'} · возраст: ${data.model?.age_days ?? '—'} дн. · источник: ${turbine.provenance?.source || '—'} · получено: ${turbine.provenance?.retrieved_at ? utcFull(turbine.provenance.retrieved_at) + ' UTC' : '—'}`;
    q('[data-current]').textContent = `Текущая оценка модели на ${current.valid_time ? utcFull(current.valid_time) + ' UTC' : '—'}: ветер 100 м ${fmt(current.wind_speed_100m)} м/с, 10 м ${fmt(current.wind_speed_10m)} м/с, температура ${fmt(current.temperature_2m)} °C. Это модельная оценка, не показание SCADA.`;
    q('[data-cutoff]').textContent = `Турбина ${state.turbine} · координаты ${turbine.coordinates?.latitude ?? '—'}, ${turbine.coordinates?.longitude ?? '—'} · номинальная мощность ${fmt(capacity, 1)} МВт`;
    q('.wd-source').textContent = `Источник: ${turbine.provenance?.source || '—'}. Погодные данные: ветер на высоте 100 м. Получено: ${turbine.provenance?.retrieved_at ? utcFull(turbine.provenance.retrieved_at) + ' UTC' : '—'}. Текущие условия — оценка модели, не измеренная генерация.`;
    q('.wd-status').textContent = `${statusBase} · горизонт ${data.horizon_hours} ч · пик ${fmt(peak?.power_mw, 2)} МВт · энергия ${fmt(energyAll)} МВт·ч`;
    root.querySelectorAll('[data-horizon]').forEach(button => button.setAttribute('aria-pressed', Number(button.dataset.horizon) === state.horizon));
    root.querySelectorAll('[data-series]').forEach(button => button.setAttribute('aria-pressed', state[button.dataset.series]));
    state.hour = Math.max(0, Math.min(state.hour, horizon - 1));
    q('#wd-hour').max = horizon - 1; q('#wd-hour').value = state.hour;
    detail(); draw(); renderWarnings();
  }
  function detail() {
    if (!data) return;
    const row = selected()?.points?.[state.hour]; if (!row || state.hour >= viewHorizon()) return;
    q('[data-hour-label]').textContent = `+${state.hour} ч`;
    q('.wd-detail').textContent = `${utc(row.timestamp)} UTC${state.power ? ` · ${fmt(row.power_mw, 2)} МВт · ${fmt(row.energy_mwh, 2)} МВт·ч` : ''}${state.wind ? ` · ${fmt(row.wind_speed)} м/с` : ''}`;
  }
  function draw() {
    if (!data || !globalThis.d3) return;
    const horizon = viewHorizon(), turbine = selected(), points = turbine?.points?.slice(0, horizon) || [], host = q('.wd-plot'), width = host.clientWidth;
    if (!width || !points.length) return;
    const height = 294, margin = {top: 29, right: 43, bottom: 43, left: 52}, x = d3.scaleLinear().domain([0, horizon]).range([margin.left, width - margin.right]);
    const capacity = number(data.capacity?.per_turbine_mw), maxPower = Math.max(capacity, ...points.map(row => number(row.upper_mw)));
    const y = d3.scaleLinear().domain([0, maxPower * 1.08]).nice().range([height - margin.bottom, margin.top]);
    const maxWind = Math.max(1, ...points.map(row => number(row.wind_speed)));
    const yw = d3.scaleLinear().domain([0, maxWind * 1.15]).nice().range([height - margin.bottom, margin.top]);
    const svg = d3.select(q('.wd-plot svg')).attr('viewBox', `0 0 ${width} ${height}`).attr('height', height); svg.selectAll('*').remove();
    svg.append('title').text(`Прогноз ${state.turbine} на ${horizon} часов. Мощность в МВт, ветер в м/с.`);
    svg.append('desc').text('Почасовые оценки модели на основе погодного прогноза. Каждая точка соответствует часовому интервалу.');
    const defs = svg.append('defs'), gradient = defs.append('linearGradient').attr('id', 'wd-area').attr('x1', '0').attr('y1', '0').attr('x2', '0').attr('y2', '1');
    gradient.append('stop').attr('offset', '0%').attr('stop-color', 'var(--gold)').attr('stop-opacity', .19); gradient.append('stop').attr('offset', '100%').attr('stop-color', 'var(--gold)').attr('stop-opacity', .01);
    svg.append('rect').attr('x', margin.left).attr('y', margin.top).attr('width', width - margin.left - margin.right).attr('height', height - margin.bottom - margin.top).attr('fill', 'none').attr('stroke', 'var(--edge)');
    y.ticks(4).forEach(t => { svg.append('line').attr('x1', margin.left).attr('x2', width - margin.right).attr('y1', y(t)).attr('y2', y(t)).attr('stroke', 'var(--edge)'); svg.append('text').attr('x', margin.left - 9).attr('y', y(t) + 4).attr('text-anchor', 'end').text(fmt(t, 1)); });
    yw.ticks(4).forEach(t => svg.append('text').attr('x', width - margin.right + 9).attr('y', yw(t) + 4).text(fmt(t, 0)));
    const ticks = width < 420 ? [0, horizon / 2, horizon] : d3.range(0, horizon + 1, horizon / 4);
    ticks.forEach(t => svg.append('text').attr('x', x(t)).attr('y', height - margin.bottom + 22).attr('text-anchor', t === 0 ? 'start' : t === horizon ? 'end' : 'middle').text(`+${t} ч`));
    svg.append('text').attr('x', margin.left).attr('y', 14).text('МВт'); svg.append('text').attr('x', width - margin.right).attr('y', 14).attr('text-anchor', 'end').text('м/с');
    svg.append('text').attr('x', width / 2).attr('y', height - 3).attr('text-anchor', 'middle').text('Часы от начала прогноза · UTC');
    const rows = points.map((row, index) => ({...row, h: index})); rows.push({...rows[rows.length - 1], h: horizon});
    const line = (key, scale) => d3.line().x(row => x(row.h)).y(row => scale(number(row[key]))).curve(d3.curveStepAfter)(rows);
    if (state.power) {
      svg.append('path').attr('d', d3.area().x(row => x(row.h)).y0(row => y(number(row.lower_mw))).y1(row => y(number(row.upper_mw))).curve(d3.curveStepAfter)(rows)).attr('fill', 'var(--gold)').attr('opacity', .16);
      svg.append('path').attr('d', d3.area().x(row => x(row.h)).y0(y(0)).y1(row => y(number(row.power_mw))).curve(d3.curveStepAfter)(rows)).attr('fill', 'url(#wd-area)');
      svg.append('path').attr('class', 'wd-power-line').attr('d', line('power_mw', y)).attr('fill', 'none').attr('stroke', 'var(--gold)').attr('stroke-width', 2.3);
    }
    if (state.wind) svg.append('path').attr('class', 'wd-wind-line').attr('d', line('wind_speed', yw)).attr('fill', 'none').attr('stroke', 'var(--teal)').attr('stroke-width', 2).attr('stroke-dasharray', '5 4');
    const guide = svg.append('line').attr('y1', margin.top).attr('y2', height - margin.bottom).attr('stroke', 'var(--sub)').attr('opacity', .5).attr('pointer-events', 'none');
    const tip = q('.wd-tooltip'); tip.hidden = true;
    svg.append('rect').attr('x', margin.left).attr('y', margin.top).attr('width', width - margin.left - margin.right).attr('height', height - margin.bottom - margin.top).attr('fill', 'transparent')
      .on('pointermove', function(event) { const [px] = d3.pointer(event, this), hour = Math.max(0, Math.min(horizon - 1, Math.floor(x.invert(px)))), row = points[hour]; guide.attr('x1', px).attr('x2', px); tip.replaceChildren(); const time = document.createElement('span'); time.className = 'wd-mono'; time.textContent = `${utcFull(row.timestamp)} UTC`; tip.append(time); if (state.power) { const value = document.createElement('div'); value.textContent = `${fmt(row.power_mw, 2)} МВт · ${fmt(row.energy_mwh, 2)} МВт·ч`; tip.append(value); } if (state.wind) { const value = document.createElement('div'); value.textContent = `${fmt(row.wind_speed)} м/с`; tip.append(value); } tip.hidden = false; tip.style.left = `${Math.max(0, Math.min(px + 14, width - tip.offsetWidth))}px`; tip.style.top = '36px'; })
      .on('pointerleave', () => { tip.hidden = true; })
      .on('click', function(event) { const [px] = d3.pointer(event, this); state.hour = Math.max(0, Math.min(horizon - 1, Math.floor(x.invert(px)))); q('#wd-hour').value = state.hour; detail(); save(); });
  }
  q('[name=turbine]').addEventListener('change', event => { state.turbine = event.target.value; save(); if (data) render(); });
  q('.wd-refresh').addEventListener('click', event => { event.preventDefault(); loadForecast({refresh: true}); });
  root.querySelectorAll('[data-horizon]').forEach(button => button.addEventListener('click', () => { const next = Number(button.dataset.horizon); if (next !== state.horizon) { state.horizon = next; state.hour = Math.min(state.hour, next - 1); save(); if (data) render(); loadForecast({refresh: false}); } }));
  root.querySelectorAll('[data-series]').forEach(button => button.addEventListener('click', () => { const key = button.dataset.series; state[key] = !state[key]; save(); render(); }));
  q('#wd-hour').addEventListener('input', event => { state.hour = Number(event.target.value); detail(); draw(); });
  q('#wd-hour').addEventListener('change', save);
  const resizeObserver = new ResizeObserver(draw); resizeObserver.observe(q('.wd-plot'));
  loadForecast({refresh: false});
  window.setInterval(() => loadForecast({refresh: true}), 300000);
})();
