/* Wind / Agent — frontend for the local API. No mock generator, CDN, or build step. */
(() => {
  'use strict';
  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => [...document.querySelectorAll(selector)];
  const NS = 'http://www.w3.org/2000/svg';
  const TZ = 'Asia/Almaty';
  const STORAGE_KEY = 'wind-agent-ui-v3';
  const coordinates = { T1: '43.645150, 78.535604', T2: '43.643198, 78.538828' };
  const state = { mode: 'live', turbine: 'T1', date: '2026-02-01', horizon: 48, hour: 12, power: true, wind: true };
  let forecast = null;
  let status = null;
  let forecastController = null;
  let forecastRequest = 0;
  let chatController = null;
  let chatRequest = 0;
  let chatHistory = [];
  let chatBusy = false;
  let chartGeometry = null;
  let resizeFrame = null;
  let liveDate = null;
  let statusRetryTimer = null;

  const validDate = (value) => /^2026-(01-31|02-(0[1-9]|1\d|2[0-8]))$/.test(value);
  const number = (value, digits = 2) => Number.isFinite(value)
    ? value.toLocaleString('ru-RU', { minimumFractionDigits: digits, maximumFractionDigits: digits }) : '—';
  const formatTime = (value, options = {}) => {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '—';
    return new Intl.DateTimeFormat('ru-RU', { timeZone: TZ, day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', ...options }).format(date);
  };
  const hourOnly = (value) => new Intl.DateTimeFormat('ru-RU', { timeZone: TZ, hour: '2-digit', minute: '2-digit' }).format(new Date(value));
  const dateShort = (value) => value.split('-').slice(1).reverse().join('.');
  const first = (value) => Array.isArray(value) ? value[0] : value;
  const average = (rows, key) => rows.reduce((sum, row) => sum + row[key], 0) / rows.length;
  const localToday = () => new Intl.DateTimeFormat('en-CA', { timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());
  const displayDate = () => state.mode === 'live' ? (liveDate || localToday()) : state.date;
  const contextLabel = () => `${state.turbine} · ${state.mode === 'live' ? 'LIVE' : dateShort(state.date)} · ${state.horizon} ч`;
  const setText = (selector, value) => { const node = $(selector); if (node) node.textContent = value; };
  const metric = (name, value) => setText(`[data-metric="${name}"]`, value);
  const point = (name, value) => setText(`[data-point="${name}"]`, value);

  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY));
    if (saved && coordinates[saved.turbine]) state.turbine = saved.turbine;
    if (saved && validDate(saved.date)) state.date = saved.date;
    if (saved && [24, 48].includes(saved.horizon)) state.horizon = saved.horizon;
  } catch { /* Private browsing can disable localStorage. */ }

  function persistSelection() {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify({ turbine: state.turbine, date: state.date, horizon: state.horizon })); } catch { /* Optional preference storage only. */ }
  }

  function updateControls() {
    document.body.dataset.mode = state.mode;
    const dateInput = $('#forecast-date');
    dateInput.disabled = state.mode === 'live';
    dateInput.min = state.mode === 'live' ? '' : '2026-01-31';
    dateInput.max = state.mode === 'live' ? '' : '2026-02-28';
    dateInput.value = displayDate();
    dateInput.setCustomValidity('');
    setText('#date-caption', state.mode === 'live' ? 'Текущая дата' : 'Дата среза');
    setText('#date-hint', state.mode === 'live' ? '· сейчас, UTC+5' : '· 00:00 UTC+5');
    $$('[data-mode]').filter((node) => node.tagName === 'BUTTON').forEach((button) => button.setAttribute('aria-pressed', String(button.dataset.mode === state.mode)));
    setText('#mode-notice', state.mode === 'live' ? 'Актуальная погода · прогноз от текущего момента' : 'Архивный прогноз · доступность погоды на выбранную историческую дату не подтверждена');
    $$('[data-turbine]').forEach((button) => button.setAttribute('aria-pressed', String(button.dataset.turbine === state.turbine)));
    $$('[data-horizon]').forEach((button) => button.setAttribute('aria-pressed', String(Number(button.dataset.horizon) === state.horizon)));
    $$('[data-series]').forEach((button) => button.setAttribute('aria-pressed', String(state[button.dataset.series])));
    setText('[data-horizon-label]', `Все ${state.horizon} ч`);
    setText('#chat-context', contextLabel());
    setText('#weather-coordinates', coordinates[state.turbine]);
    setText('#hour-label', `+${state.hour} ч`);
    $('#forecast-hour').max = state.horizon - 1;
    $('#forecast-hour').value = state.hour;
  }

  async function requestJSON(url, options = {}, timeout = 90000) {
    const timeoutController = new AbortController();
    const timer = setTimeout(() => timeoutController.abort(), timeout);
    const abort = () => timeoutController.abort();
    if (options.signal?.aborted) timeoutController.abort();
    else options.signal?.addEventListener('abort', abort, { once: true });
    try {
      const response = await fetch(url, { ...options, signal: timeoutController.signal, headers: { Accept: 'application/json', ...options.headers } });
      const contentType = response.headers.get('content-type') || '';
      if (!contentType.includes('application/json')) throw new Error('API вернул не JSON. Откройте сайт через запущенный локальный сервер API.');
      const result = await response.json();
      if (!response.ok) {
        const detail = result.detail || result.message || `Ошибка API ${response.status}`;
        throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
      }
      return result;
    } catch (error) {
      if (error.name === 'AbortError' && !options.signal?.aborted) throw new Error('Сервер не ответил вовремя. Проверьте доступ к погодному API и повторите запрос.');
      if (error instanceof TypeError) throw new Error('Нет соединения с API. Запустите сервер и откройте сайт по адресу http://127.0.0.1:8000.');
      throw error;
    } finally {
      clearTimeout(timer);
      options.signal?.removeEventListener('abort', abort);
    }
  }

  function connection(text, error = false) {
    setText('#connection-label', text);
    $('.connection-badge').classList.toggle('is-error', error);
  }

  function clearForecastView(loading) {
    forecast = null;
    chartGeometry = null;
    $$('[data-metric]').forEach((node) => { node.textContent = '—'; });
    $$('[data-point]').forEach((node) => { node.textContent = '—'; });
    $('#forecast-chart').replaceChildren();
    $('#chart-tooltip').hidden = true;
    $('#forecast-hour').disabled = true;
    $('#export-button').disabled = true;
    $('#interval-legend').hidden = true;
    $('#chart-empty').hidden = false;
    $('#chart-empty').replaceChildren();
    if (loading) {
      const loader = document.createElement('span'); loader.className = 'loader';
      $('#chart-empty').append(loader);
    }
    const label = document.createElement('p');
    label.textContent = loading ? (state.mode === 'live' ? 'Получаем актуальную погоду и запускаем модель…' : 'Получаем архивную погоду и запускаем модель…') : 'Прогноз недоступен. Проверьте сообщение выше.';
    $('#chart-empty').append(label);
    setText('#chart-period', `${state.turbine} · ${state.mode === 'live' ? 'LIVE' : dateShort(state.date)} · UTC+5`);
    ['#weather-source', '#weather-run', '#weather-available'].forEach((selector) => setText(selector, '—'));
    setText('#warning-count', '—');
    $('#warnings-list').replaceChildren();
    const note = document.createElement('p'); note.className = 'muted'; note.textContent = loading ? 'Выполняется проверка входных данных…' : 'Расчёт не завершён. Данные не подменяются демо-прогнозом.';
    $('#warnings-list').append(note);
    setText('#cutoff-label', loading ? 'Проверяем момент доступности данных…' : 'Нет успешного прогноза для выбранного среза');
    setText('#forecast-metadata', `${state.mode} · ${state.turbine} · ${state.horizon} h`);
  }

  function validateForecast(result) {
    if (!result || !Array.isArray(result.forecast) || result.forecast.length !== state.horizon) throw new Error(`API должен вернуть ${state.horizon} почасовых значений.`);
    if (result.turbine_id !== state.turbine || (state.mode === 'backtest' && result.as_of_date !== state.date) || (state.mode === 'live' && result.mode !== 'live')) throw new Error('API вернул прогноз для другой турбины или даты. Повторите запрос.');
    if (result.power_unit && result.power_unit !== 'normalized') throw new Error('Неожиданные единицы мощности: интерфейс ожидает нормализованные значения.');
    result.forecast.forEach((row, index) => {
      if (!['predicted_power', 'wind_speed', 'temperature'].every((key) => Number.isFinite(row[key])) || !Number.isFinite(Date.parse(row.timestamp))) throw new Error('В прогнозе есть пропуски или некорректные числовые значения.');
      if (row.predicted_power < 0 || row.predicted_power > 1) throw new Error('Нормализованная мощность вне допустимого диапазона 0–1.');
      if (index && Date.parse(row.timestamp) - Date.parse(result.forecast[index - 1].timestamp) !== 3600000) throw new Error('Почасовая сетка прогноза нарушена.');
      if (row.lower !== undefined || row.upper !== undefined) {
        if (!Number.isFinite(row.lower) || !Number.isFinite(row.upper) || row.lower < 0 || row.upper > 1 || row.lower > row.predicted_power || row.upper < row.predicted_power) throw new Error('API вернул некорректные границы интервала мощности.');
      }
    });
    return result;
  }

  async function loadForecast(refresh = false) {
    if (state.mode === 'backtest' && !validDate(state.date)) return;
    forecastController?.abort();
    forecastController = new AbortController();
    const controller = forecastController;
    const requestId = ++forecastRequest;
    $('#forecast-error').hidden = true;
    document.body.classList.add('is-busy');
    $('#forecast').setAttribute('aria-busy', 'true');
    $('#refresh-button').disabled = true;
    $('#refresh-button').classList.add('is-loading');
    setText('#refresh-button span', 'Рассчитываем…');
    connection('Расчёт прогноза');
    clearForecastView(true);
    updateControls();
    renderModel();
    const params = new URLSearchParams({ mode: state.mode, turbine_id: state.turbine, horizon_hours: String(state.horizon), refresh: String(refresh) });
    if (state.mode === 'backtest') params.set('as_of_date', state.date);
    try {
      const result = await requestJSON(`/api/forecast?${params}`, { signal: controller.signal });
      if (requestId !== forecastRequest) return;
      forecast = validateForecast(result);
      if (state.mode === 'live') liveDate = forecast.as_of_date;
      updateControls();
      renderForecast();
      connection('API подключён · модель готова');
    } catch (error) {
      if (requestId !== forecastRequest || error.name === 'AbortError') return;
      clearForecastView(false);
      const heading = document.createElement('strong'); heading.textContent = 'Не удалось построить прогноз';
      const message = document.createElement('p'); message.textContent = error.message;
      const hint = document.createElement('p'); hint.textContent = 'Выберите другой срез или повторите запрос. Отсутствующие данные не заменяются синтетическими.';
      $('#forecast-error').replaceChildren(heading, message, hint);
      $('#forecast-error').hidden = false;
      connection('Прогноз недоступен', true);
    } finally {
      if (requestId === forecastRequest) {
        document.body.classList.remove('is-busy');
        $('#forecast').setAttribute('aria-busy', 'false');
        $('#refresh-button').disabled = false;
        $('#refresh-button').classList.remove('is-loading');
        setText('#refresh-button span', 'Пересчитать');
      }
    }
  }

  function renderForecast() {
    const rows = forecast.forecast;
    const peak = rows.reduce((best, row) => row.predicted_power > best.predicted_power ? row : best);
    const wind = rows.map((row) => row.wind_speed);
    metric('mean24', number(average(rows.slice(0, 24), 'predicted_power'), 3));
    metric('meanAll', number(average(rows, 'predicted_power'), 3));
    metric('peak', number(peak.predicted_power, 3));
    metric('peakTime', `${formatTime(peak.timestamp)} · UTC+5`);
    metric('wind', number(average(rows, 'wind_speed'), 1));
    metric('windRange', `${number(Math.min(...wind), 1)} — ${number(Math.max(...wind), 1)} м/с за ${state.horizon} ч`);
    metric('weatherWind', number(average(rows, 'wind_speed'), 1));
    metric('temperature', number(average(rows, 'temperature'), 1));
    metric('maxWind', number(Math.max(...wind), 1));
    renderCapacityEstimate(rows, peak);
    setText('#chart-period', `${dateShort(forecast.as_of_date)} · UTC+5`);
    $('#chart-empty').hidden = true;
    $('#forecast-hour').disabled = false;
    $('#export-button').disabled = false;
    const provenance = forecast.provenance || {};
    setText('#weather-source', first(provenance.source) || 'Источник не указан');
    const isLive = state.mode === 'live';
    const retrieved = first(provenance.retrieved_at);
    setText('#weather-run-label', isLive ? 'Погодные данные получены' : 'Выпуск модели погоды');
    setText('#weather-available-label', isLive ? 'Свежесть данных' : 'Доступность по допущению');
    setText('#weather-run', isLive ? (retrieved ? `${formatTime(retrieved)} · UTC+5` : 'Время получения не передано') : (first(provenance.initialized_at) ? `${formatTime(first(provenance.initialized_at))} · UTC+5` : 'Время выпуска не указано'));
    const cache = provenance.cache || forecast.cache || {};
    setText('#weather-available', isLive ? (Number.isFinite(cache.age_seconds) ? `${Math.round(cache.age_seconds / 60)} мин · ${cache.status || 'текущий запрос'}` : 'Получены для текущего запроса') : (first(provenance.available_at) ? `${formatTime(first(provenance.available_at))} · UTC+5` : 'Не подтверждена'));
    setText('#weather-source-tag', isLive ? 'Актуальный прогноз' : (forecast.as_of_verified === true ? 'Архивный выпуск' : 'Доступность не подтверждена'));
    setText('#cutoff-label', isLive ? `Live · прогноз от ${formatTime(forecast.origin)} UTC+5` : `Архивный срез: ${formatTime(forecast.origin || `${state.date}T00:00:00+05:00`)} UTC+5 · доступность исторической погоды не подтверждена`);
    setText('#provenance-note', isLive ? 'Используется актуальный погодный прогноз. Live-режим не воспроизводит исторический срез. Нормализованная модель обучена на данных до февраля 2026 года; точность на текущем сезоне не подтверждена.' : 'Для архива используется допущение о задержке публикации. Историческая доступность выпуска не подтверждена.');
    setText('#integrity-note', isLive ? 'Актуальный прогноз погоды подаётся в обученную модель. Ошибки погоды и изменение условий после обучения требуют отдельной проверки.' : 'Часовой пояс, нормализация мощности и высота ветра требуют подтверждения. Все ограничения доступны в карточке данных.');
    setText('#forecast-metadata', `as_of_date ${forecast.as_of_date}\ngenerated_at ${forecast.generated_at || '—'}\nmodel ${forecast.model?.version || '—'} · run ${forecast.forecast_id ?? '—'}`);
    renderWarnings();
    renderPoint();
    drawChart();
  }

  function renderCapacityEstimate(rows, peak) {
    const panel = $('#capacity-summary');
    const conversion = forecast.energy_conversion || {};
    const configuredCapacity = Number(forecast.capacity_mw ?? forecast.capacity?.per_turbine_mw);
    const enabled = state.mode === 'live' && conversion.enabled === true && Number.isFinite(configuredCapacity) && configuredCapacity > 0;
    panel.hidden = !enabled;
    if (!enabled) { $('#farm-summary').hidden = true; return; }

    const energyFor = (items) => items.reduce((sum, row) => sum + row.predicted_power * configuredCapacity, 0);
    setText('[data-capacity="mean-mw"]', number(average(rows, 'predicted_power') * configuredCapacity, 2));
    setText('[data-capacity="energy24"]', number(energyFor(rows.slice(0, 24)), 1));
    setText('[data-capacity="energy-horizon"]', number(energyFor(rows), 1));
    setText('[data-capacity="peak-mw"]', number(peak.predicted_power * configuredCapacity, 2));

    const farm = forecast.farm || {};
    const farmPoints = Array.isArray(farm.forecast) ? farm.forecast : Array.isArray(farm.points) ? farm.points : [];
    const farmEnergy24 = Number(farm.energy_24h_mwh ?? farm.summary?.energy_24h_mwh ?? forecast.farm_energy_24h_mwh);
    const farmEnergyHorizon = Number(farm.energy_horizon_mwh ?? farm.total_energy_horizon_mwh ?? farm.summary?.energy_horizon_mwh ?? forecast.farm_energy_horizon_mwh);
    const farmHasPoints = farmPoints.length >= rows.length && farmPoints.slice(0, rows.length).every((row) => Number.isFinite(row.energy_mwh));
    const energy24 = Number.isFinite(farmEnergy24) ? farmEnergy24 : farmHasPoints ? farmPoints.slice(0, 24).reduce((sum, row) => sum + row.energy_mwh, 0) : NaN;
    const energyHorizon = Number.isFinite(farmEnergyHorizon) ? farmEnergyHorizon : farmHasPoints ? farmPoints.slice(0, rows.length).reduce((sum, row) => sum + row.energy_mwh, 0) : NaN;
    const farmReady = Number.isFinite(energy24) && Number.isFinite(energyHorizon);
    $('#farm-summary').hidden = !farmReady;
    if (farmReady) {
      setText('[data-farm="energy24"]', number(energy24, 1));
      setText('[data-farm="energy-horizon"]', number(energyHorizon, 1));
    }
    setText('.capacity-badge', `${number(configuredCapacity, 1)} МВт · данные пользователя`);
    const basis = conversion.basis === 'assumed_rated_capacity_fraction' ? 'Допущение: нормализованная мощность 0–1 пропорциональна доле номинальной мощности.' : 'Способ пересчёта нормализованной мощности требует проверки.';
    setText('.capacity-note', `Оценка получена умножением нормализованной мощности на ${number(configuredCapacity, 1)} МВт на каждом часовом интервале. ${basis} Это прогноз модели, не измеренная выработка.`);
  }

  const translatedWarnings = {
    'Capacity unavailable': ['Нормализованная мощность', 'Формула нормализации и номинальная мощность не подтверждены. Перевод в МВт и МВт·ч недоступен.'],
    'Weather height assumption': ['Высота ветра — допущение', 'Используется прогноз ветра ECMWF на 100 м. Высота ступицы и локальная поправка требуют проверки.'],
    'Conditional uncertainty only': ['Интервал не включает ошибку погоды', 'Границы отражают исторические остатки модели при заданной погоде, а не полную неопределённость прогноза.']
  };

  function renderWarnings() {
    let warnings = forecast.warning_details || (forecast.warnings || []).map((message) => ({ title: 'Сообщение агента', message, severity: 'warning' }));
    warnings = warnings.map((warning) => {
      const translated = translatedWarnings[warning.title];
      return translated ? { ...warning, title: translated[0], message: translated[1] } : warning;
    });
    if (state.mode === 'backtest' && forecast.as_of_verified !== true && !warnings.some((warning) => /доступност|hindcast|publication|provenance/i.test(`${warning.title} ${warning.message}`))) {
      warnings = [{ title: 'Архив требует проверки', message: 'Время доступности получено по допущению о задержке публикации. Не подтверждено, что эти значения были опубликованы в выбранный момент прошлого.', severity: 'warning' }, ...warnings];
    }
    setText('#warning-count', String(warnings.length));
    $('#warnings-list').replaceChildren(...warnings.map((warning) => {
      const item = document.createElement('article'); item.className = `warning-item ${warning.severity === 'info' ? 'info' : ''}`;
      const symbol = document.createElement('span'); symbol.className = 'warning-symbol'; symbol.setAttribute('aria-hidden', 'true'); symbol.textContent = warning.severity === 'info' ? '◌' : '△';
      const content = document.createElement('div'); const title = document.createElement('h3'); title.textContent = warning.title || 'Проверка агента';
      const message = document.createElement('p'); message.textContent = warning.message || '';
      content.append(title, message); item.append(symbol, content); return item;
    }));
  }

  async function loadStatus(attempt = 0) {
    clearTimeout(statusRetryTimer);
    try {
      status = await requestJSON('/api/status', {}, 20000);
      $('#model-retry')?.remove();
      renderModel();
      setChatMode(status.chat?.mode || 'local');
      const liveButton = $('button[data-mode="live"]');
      if (status.capabilities?.live === false) {
        liveButton.disabled = true;
        liveButton.title = 'В этом сохранённом просмотре доступны только архивные прогнозы. Для live запустите сервер.';
      }
    } catch (error) {
      if (attempt === 0) {
        setText('#model-name', 'Повторное подключение к модели…');
        statusRetryTimer = setTimeout(() => loadStatus(1), 1200);
        return;
      }
      setText('#model-name', 'Метаданные временно недоступны');
      $('#model-name').title = error.message;
      if (!$('#model-retry')) {
        const retry = document.createElement('button'); retry.type = 'button'; retry.id = 'model-retry'; retry.className = 'model-retry'; retry.textContent = 'Повторить';
        retry.addEventListener('click', () => { retry.remove(); loadStatus(); });
        $('#model-name').after(retry);
      }
    }
  }

  function renderModel() {
    if (!status) return;
    const model = status.model?.turbines ? status.model : status.metadata?.turbines ? status.metadata : status;
    const turbineNumber = state.turbine === 'T1' ? '1' : '2';
    const turbine = model.turbines?.[turbineNumber] || {};
    const holdout = turbine.holdout || model.metrics?.[turbineNumber]?.holdout || {};
    setText('[data-model="mae"]', number(holdout.mae, 4));
    setText('[data-model="rmse"]', number(holdout.rmse, 4));
    setText('[data-model="hours"]', number(turbine.training_hours, 0));
    setText('#model-name', `${state.turbine} / ${turbine.selected_candidate || model.model_version || 'Модель не указана'}`);
    setText('#model-period', turbine.training_last_hour_utc ? `Обучение по ${formatTime(turbine.training_last_hour_utc, { hour: undefined, minute: undefined, year: 'numeric' })}` : 'Исторический ряд турбины');
  }

  function renderPoint() {
    if (!forecast) return;
    state.hour = Math.min(state.hour, forecast.forecast.length - 1);
    const row = forecast.forecast[state.hour];
    point('time', formatTime(row.timestamp));
    point('power', number(row.predicted_power, 3));
    point('wind', number(row.wind_speed, 1));
    point('temperature', number(row.temperature, 1));
    $('#forecast-hour').value = state.hour;
    setText('#hour-label', `+${state.hour} ч`);
    updateGuide(state.hour);
  }

  function svgElement(name, attributes = {}, text) {
    const node = document.createElementNS(NS, name);
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function drawChart() {
    if (!forecast) return;
    const svg = $('#forecast-chart');
    const width = Math.max(260, $('#chart-wrap').clientWidth);
    const height = $('#chart-wrap').clientHeight;
    const margin = { top: 30, right: 33, bottom: 40, left: 38 };
    const plotWidth = width - margin.left - margin.right;
    const plotHeight = height - margin.top - margin.bottom;
    const rows = forecast.forecast;
    const windMax = Math.max(5, Math.ceil(Math.max(...rows.map((row) => row.wind_speed)) / 5) * 5);
    const x = (hour) => margin.left + hour / (rows.length - 1) * plotWidth;
    const y = (value) => height - margin.bottom - value * plotHeight;
    const yw = (value) => y(value / windMax);
    const path = (values, scale) => values.map((value, index) => `${index ? 'L' : 'M'}${x(index).toFixed(2)},${scale(value).toFixed(2)}`).join(' ');
    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    svg.replaceChildren();
    svg.append(svgElement('title', {}, `${state.turbine}: прогноз мощности и ветра на ${state.horizon} часов`));
    svg.append(svgElement('desc', {}, `Мощность в нормализованной шкале 0–1. Ветер в метрах в секунду. Все отметки времени Asia/Almaty. Для выбора часа используйте ползунок под графиком.`));
    const defs = svgElement('defs');
    const gradient = svgElement('linearGradient', { id: 'power-fill', x1: 0, y1: 0, x2: 0, y2: 1 });
    gradient.append(svgElement('stop', { offset: '0%', 'stop-color': '#86c5d9', 'stop-opacity': '.19' }), svgElement('stop', { offset: '100%', 'stop-color': '#235c9e', 'stop-opacity': '.01' }));
    defs.append(gradient); svg.append(defs);
    for (let tick = 0; tick <= 4; tick += 1) {
      const value = tick / 4;
      svg.append(svgElement('line', { x1: margin.left, x2: width - margin.right, y1: y(value), y2: y(value), stroke: '#1d3450', 'stroke-width': 1, 'stroke-dasharray': tick ? '2 5' : '' }));
      svg.append(svgElement('text', { x: margin.left - 10, y: y(value) + 3, 'text-anchor': 'end' }, number(value, 2)));
      svg.append(svgElement('text', { x: width - margin.right + 9, y: y(value) + 3 }, number(value * windMax, value * windMax % 1 ? 1 : 0)));
    }
    svg.append(svgElement('text', { x: margin.left, y: 13 }, 'МОЩНОСТЬ · НОРМ.'));
    svg.append(svgElement('text', { x: width - margin.right, y: 13, 'text-anchor': 'end' }, 'ВЕТЕР · М/С'));
    const ticks = width < 430 ? [0, Math.round((rows.length - 1) / 2), rows.length - 1] : [0, Math.round((rows.length - 1) / 4), Math.round((rows.length - 1) / 2), Math.round((rows.length - 1) * .75), rows.length - 1];
    ticks.forEach((index) => {
      svg.append(svgElement('text', { x: x(index), y: height - margin.bottom + 19, 'text-anchor': 'middle' }, hourOnly(rows[index].timestamp)));
      svg.append(svgElement('text', { x: x(index), y: height - margin.bottom + 31, 'text-anchor': 'middle', opacity: .65, style: 'font-size:7px' }, formatTime(rows[index].timestamp, { hour: undefined, minute: undefined })));
    });
    if (rows.length > 24) {
      svg.append(svgElement('rect', { x: x(24), y: margin.top, width: width - margin.right - x(24), height: plotHeight, fill: '#3469ce', opacity: '.035' }));
      svg.append(svgElement('line', { x1: x(24), x2: x(24), y1: margin.top, y2: height - margin.bottom, stroke: '#5083b5', 'stroke-dasharray': '3 5', opacity: '.5' }));
      svg.append(svgElement('rect', { x: x(24) - 20, y: margin.top - 2, width: 40, height: 17, rx: 4, fill: '#122c48', stroke: '#2d5578' }));
      svg.append(svgElement('text', { x: x(24), y: margin.top + 9, 'text-anchor': 'middle', style: 'fill:#95c6e6;font-size:8px' }, '+24 ч'));
    }
    const hasInterval = rows.every((row) => Number.isFinite(row.lower) && Number.isFinite(row.upper));
    $('#interval-legend').hidden = !hasInterval || !state.power;
    if (state.power) {
      svg.append(svgElement('path', { d: `${path(rows.map((row) => row.predicted_power), y)} L${x(rows.length - 1)},${y(0)} L${x(0)},${y(0)} Z`, fill: 'url(#power-fill)' }));
      if (hasInterval) {
        const upper = path(rows.map((row) => row.upper), y);
        const lower = rows.map((row, index) => `L${x(index).toFixed(2)},${y(row.lower).toFixed(2)}`).reverse().join(' ');
        svg.append(svgElement('path', { d: `${upper} ${lower} Z`, fill: '#9dcfe1', opacity: '.10' }));
      }
      svg.append(svgElement('path', { d: path(rows.map((row) => row.predicted_power), y), fill: 'none', stroke: '#acdce9', 'stroke-width': 2.3, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
    }
    if (state.wind) svg.append(svgElement('path', { d: path(rows.map((row) => row.wind_speed), yw), fill: 'none', stroke: '#659ace', 'stroke-width': 1.7, 'stroke-dasharray': '5 5', 'stroke-linejoin': 'round', 'stroke-linecap': 'round', opacity: .92 }));
    const guide = svgElement('g', { id: 'chart-guide', 'pointer-events': 'none' });
    guide.append(svgElement('line', { id: 'guide-line', y1: margin.top, y2: height - margin.bottom, stroke: '#99cce2', opacity: .32, 'stroke-dasharray': '2 4' }));
    if (state.power) guide.append(svgElement('circle', { id: 'power-marker', r: 4, fill: '#bdeeff', stroke: '#193759', 'stroke-width': 2 }));
    if (state.wind) guide.append(svgElement('circle', { id: 'wind-marker', r: 3, fill: '#71a1ff', stroke: '#152841', 'stroke-width': 2 }));
    svg.append(guide);
    chartGeometry = { x, y, yw, margin, width, height, plotWidth, count: rows.length };
    updateGuide(state.hour);
    const overlay = svgElement('rect', { x: margin.left, y: margin.top, width: plotWidth, height: plotHeight, fill: 'transparent', 'data-chart-hover-overlay': 'cross-series' });
    overlay.addEventListener('pointermove', (event) => {
      const index = pointerIndex(event);
      updateGuide(index);
      showTooltip(index, event);
    });
    overlay.addEventListener('pointerleave', () => { $('#chart-tooltip').hidden = true; updateGuide(state.hour); });
    overlay.addEventListener('click', (event) => { state.hour = pointerIndex(event); renderPoint(); });
    svg.append(overlay);
  }

  function pointerIndex(event) {
    const rect = $('#forecast-chart').getBoundingClientRect();
    const position = (event.clientX - rect.left) / rect.width * chartGeometry.width;
    return Math.max(0, Math.min(chartGeometry.count - 1, Math.round((position - chartGeometry.margin.left) / chartGeometry.plotWidth * (chartGeometry.count - 1))));
  }

  function updateGuide(index) {
    if (!chartGeometry || !forecast) return;
    const row = forecast.forecast[index];
    const position = chartGeometry.x(index);
    const line = $('#guide-line');
    if (line) { line.setAttribute('x1', position); line.setAttribute('x2', position); }
    const power = $('#power-marker');
    if (power) { power.setAttribute('cx', position); power.setAttribute('cy', chartGeometry.y(row.predicted_power)); }
    const wind = $('#wind-marker');
    if (wind) { wind.setAttribute('cx', position); wind.setAttribute('cy', chartGeometry.yw(row.wind_speed)); }
  }

  function showTooltip(index) {
    const row = forecast.forecast[index];
    const tip = $('#chart-tooltip');
    const title = document.createElement('strong'); title.textContent = `${formatTime(row.timestamp)} · UTC+5`;
    const values = [];
    if (state.power) values.push(['Мощность', `${number(row.predicted_power, 3)} норм.`]);
    if (state.wind) values.push(['Ветер', `${number(row.wind_speed, 1)} м/с`]);
    values.push(['Температура', `${number(row.temperature, 1)} °C`]);
    tip.replaceChildren(title, ...values.map(([label, value]) => {
      const line = document.createElement('div');
      const name = document.createElement('span'); name.textContent = label;
      const result = document.createElement('span'); result.textContent = value;
      line.append(name, result); return line;
    }));
    tip.hidden = false;
    tip.style.left = `${Math.max(0, Math.min(chartGeometry.x(index) + 14, chartGeometry.width - tip.offsetWidth))}px`;
    tip.style.top = '35px';
  }

  function setChatMode(mode) {
    const usesLLM = typeof mode === 'string' && /llm|openai|remote/i.test(mode);
    setText('#chat-mode', usesLLM ? 'AI-аналитик · LLM' : 'Локальный аналитик');
    setText('#composer-mode', usesLLM ? '/ LLM' : '/ local');
    setText('#chat-disclaimer', usesLLM ? 'LLM объясняет данные. Сверяйте выводы с прогнозом и метриками.' : 'Локальный анализ данных по правилам · без LLM');
  }

  function appendMessage(role, text, note = '', error = false) {
    const article = document.createElement('article'); article.className = `chat-message ${role === 'user' ? 'user-message' : 'assistant-message'}${error ? ' is-error' : ''}`;
    const author = document.createElement('span'); author.className = 'message-author'; author.textContent = role === 'user' ? 'ВЫ' : 'AGENTIC AI';
    const content = document.createElement('p'); content.textContent = text;
    article.append(author, content);
    if (note) { const annotation = document.createElement('p'); annotation.className = 'message-note'; annotation.textContent = note; article.append(annotation); }
    $('#chat-messages').append(article);
    $('#chat-messages').scrollTop = $('#chat-messages').scrollHeight;
    return article;
  }

  function setChatBusy(value) {
    chatBusy = value;
    $('#chat-send').disabled = value;
    $$('[data-prompt]').forEach((button) => { button.disabled = value; });
    $('#chat-messages').setAttribute('aria-busy', String(value));
  }

  async function sendChat(message) {
    message = message.trim();
    if (!message || chatBusy) return;
    const current = { mode: state.mode, turbine_id: state.turbine, horizon_hours: state.horizon };
    if (state.mode === 'backtest') current.as_of_date = state.date;
    const label = contextLabel();
    const requestId = ++chatRequest;
    chatController = new AbortController();
    const controller = chatController;
    appendMessage('user', message, label);
    const history = chatHistory.slice(-10);
    $('#chat-input').value = '';
    const pending = document.createElement('div'); pending.className = 'typing'; pending.setAttribute('aria-label', 'Agentic AI готовит ответ');
    for (let index = 0; index < 3; index += 1) pending.append(document.createElement('span'));
    $('#chat-messages').append(pending);
    $('#chat-messages').scrollTop = $('#chat-messages').scrollHeight;
    setChatBusy(true);
    try {
      const response = await requestJSON('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: controller.signal, body: JSON.stringify({ message, ...current, history }) });
      if (requestId !== chatRequest) return;
      if (typeof response.reply !== 'string' || !response.reply.trim()) throw new Error('Ассистент вернул пустой ответ. Попробуйте повторить вопрос.');
      pending.remove();
      appendMessage('assistant', response.reply, label !== contextLabel() ? `Ответ для ${label}` : '');
      chatHistory.push({ role: 'user', content: message }, { role: 'assistant', content: response.reply });
      chatHistory = chatHistory.slice(-12);
      setChatMode(response.mode || 'local');
      if (Array.isArray(response.actions) && response.actions.some((action) => ['forecast_refreshed', 'recalculated'].includes(typeof action === 'string' ? action : action.type)) && label === contextLabel()) loadForecast(false);
    } catch (error) {
      if (requestId !== chatRequest || error.name === 'AbortError') return;
      pending.remove();
      appendMessage('assistant', error.message, 'Ответ не был сгенерирован. Проверьте сервер и повторите вопрос.', true);
    } finally {
      if (requestId === chatRequest) { pending.remove(); setChatBusy(false); }
    }
  }

  function exportCSV() {
    if (!forecast) return;
    const header = ['turbine_id', 'as_of_date', 'timestamp_utc', 'predicted_power_normalized', 'lower_conditional', 'upper_conditional', 'wind_speed_ms', 'temperature_c'];
    const rows = forecast.forecast.map((row) => [forecast.turbine_id, forecast.as_of_date, new Date(row.timestamp).toISOString(), row.predicted_power, row.lower ?? '', row.upper ?? '', row.wind_speed, row.temperature].join(','));
    const blob = new Blob(['\uFEFF', header.join(','), '\n', rows.join('\n')], { type: 'text/csv;charset=utf-8' });
    const link = document.createElement('a'); const url = URL.createObjectURL(blob);
    link.href = url; link.download = `forecast-${forecast.turbine_id}-${forecast.as_of_date}-${state.horizon}h.csv`;
    document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  $$('button[data-mode]').forEach((button) => button.addEventListener('click', () => {
    if (button.disabled || state.mode === button.dataset.mode) return;
    state.mode = button.dataset.mode;
    liveDate = null;
    updateControls();
    loadForecast();
  }));
  $('.mode-notice a').addEventListener('click', () => { $('#weather-provenance').open = true; });
  $$('[data-turbine]').forEach((button) => button.addEventListener('click', () => {
    if (state.turbine === button.dataset.turbine) return;
    state.turbine = button.dataset.turbine; updateControls(); persistSelection(); loadForecast();
  }));
  $('#forecast-date').addEventListener('change', (event) => {
    if (!validDate(event.target.value)) { event.target.setCustomValidity('Выберите дату с 31 января по 28 февраля 2026 года.'); event.target.reportValidity(); return; }
    event.target.setCustomValidity('');
    if (state.date === event.target.value) return;
    state.date = event.target.value; updateControls(); persistSelection(); loadForecast();
  });
  $$('[data-horizon]').forEach((button) => button.addEventListener('click', () => {
    const horizon = Number(button.dataset.horizon);
    if (horizon === state.horizon) return;
    state.horizon = horizon; state.hour = Math.min(state.hour, horizon - 1); updateControls(); persistSelection(); loadForecast();
  }));
  $$('[data-series]').forEach((button) => button.addEventListener('click', () => {
    state[button.dataset.series] = !state[button.dataset.series]; updateControls(); drawChart();
  }));
  $('#forecast-hour').addEventListener('input', (event) => { state.hour = Number(event.target.value); renderPoint(); });
  $('#refresh-button').addEventListener('click', () => { if ($('#forecast-date').reportValidity()) loadForecast(true); });
  $('#export-button').addEventListener('click', exportCSV);
  $('#chat-form').addEventListener('submit', (event) => { event.preventDefault(); sendChat($('#chat-input').value); });
  $('#chat-input').addEventListener('keydown', (event) => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); sendChat(event.target.value); } });
  $$('[data-prompt]').forEach((button) => button.addEventListener('click', () => sendChat(button.dataset.prompt)));
  $('#clear-chat').addEventListener('click', () => {
    chatController?.abort(); chatRequest += 1; chatHistory = []; setChatBusy(false);
    $('#chat-messages').replaceChildren(); appendMessage('assistant', 'Новый диалог. Разберём прогноз, качество модели или доступность данных.', 'Контекст — выбранная турбина и дата среза.');
    $('#chat-input').value = ''; $('#chat-input').focus();
  });
  new ResizeObserver(() => { cancelAnimationFrame(resizeFrame); resizeFrame = requestAnimationFrame(drawChart); }).observe($('#chart-wrap'));
  updateControls();
  if (location.protocol === 'file:') {
    clearForecastView(false);
    $('#forecast-error').textContent = 'Для реальной модели нужен API. Запустите проект через Python и откройте http://127.0.0.1:8000. Инструкция — в README.md.';
    $('#forecast-error').hidden = false;
    connection('Откройте сайт через API', true);
  } else {
    loadStatus();
    loadForecast();
  }
})();
