"""Export real stored predictions into a portable, explicitly offline HTML viewer."""
from __future__ import annotations
import json
import base64
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from windagent import service
from windagent.chat import _local_reply

def build():
    metadata = service.status()
    metadata["capabilities"]["live"] = False
    snapshots = {}
    questions = {'compare':'Сравни турбины T1 и T2', 'window':'Найди лучшее окно из 3 часов', 'ramp':'Когда самый резкий скачок мощности?', 'days':'Сравни завтра и сегодня', 'halves':'Сравни первые 24 и вторые 24 часа', 'accuracy':'Какая точность модели?', 'training':'Как настроить модель?', 'asof':'Есть ли утечка?', 'weather':'Какой ветер?', 'summary':'Объясни прогноз', 'other':'Помоги с другим вопросом'}
    for day in range(1,29):
        date = f'2026-02-{day:02d}'
        day_forecasts = {t: service.dashboard_forecast(t, date, 48) for t in ('T1','T2')}
        for turbine in ('T1','T2'):
            complete = day_forecasts[turbine]
            for horizon in (24,48):
                data = {**complete, 'forecast':complete['forecast'][:horizon], 'horizon_hours':horizon}
                data['fleet_summary'] = service.fleet_summary({key[-1]: [{'timestamp':r['timestamp'],'power':r['predicted_power']} for r in value['forecast'][:horizon]] for key,value in day_forecasts.items()})
                snapshots[f'{turbine}:{date}:{horizon}'] = {'forecast':data,'answers':{name:_local_reply(message,data,metadata['model']) for name,message in questions.items()}}
    payload = json.dumps({'status':metadata,'snapshots':snapshots},ensure_ascii=False,separators=(',',':')).replace('<','\\u003c')
    adapter = r'''
const OFFLINE_DATA = __DATA__;
window.fetch = async (url, options={}) => {
  const address = new URL(url, 'http://offline.local');
  let value, code=200;
  if(address.pathname==='/api/status') value=OFFLINE_DATA.status;
  else {
    const input = options.body ? JSON.parse(options.body) : Object.fromEntries(address.searchParams);
    const key = `${input.turbine_id}:${input.as_of_date}:${input.horizon_hours||48}`;
    const snapshot=OFFLINE_DATA.snapshots[key];
    if(input.mode==='live'){code=422;value={detail:'Свежая погода доступна после запуска Python-сервера. Здесь сохранённый архив февраля.'};}
    else if(!snapshot){code=422;value={detail:'В офлайн-просмотре доступны сохранённые прогнозы с 1 по 28 февраля. Для другого среза запустите Python-сервер.'};}
    else if(input.refresh==='true'){code=422;value={detail:'Офлайн-просмотр не запускает модель заново. Для пересчёта откройте основной сайт через start.sh / start.ps1.'};}
    else if(address.pathname==='/api/forecast') value=snapshot.forecast;
    else if(address.pathname==='/api/chat') {
      const query=(input.message||'').toLowerCase();
      const groups=[['compare',['сравни турбин','сравнить турбин','t1 и t2','две турбин','какая турбин']],['window',['окно','3 часа','3 часов','трёхчас','трехчас']],['ramp',['скач','перепад','резк','ramp']],['halves',['первые 24','вторые 24']],['days',['сегодня','завтра','два дня']],['asof',['утеч','будущ','as_of','архив','предупреж','огранич','доступност','проверь данн']],['accuracy',['точност','ошиб','mae','метрик','качеств']],['training',['обуч','настро','параметр','улучш']],['weather',['ветер','погод','температур']],['summary',['прогноз','пик','мощност','сводк','энерги']]];
      const topic=groups.find(([,words])=>words.some(word=>query.includes(word)))?.[0]||'other';
      value={reply:snapshot.answers[topic],mode:'local',actions:[],context:{offline:true}};
    } else {code=404;value={detail:'Offline endpoint not available'};}
  }
  return new Response(JSON.stringify(value),{status:code,headers:{'Content-Type':'application/json'}});
};
'''.replace('__DATA__',payload)
    app=(ROOT/'app.js').read_text(encoding='utf-8').replace("if (location.protocol === 'file:')",'if (false)').replace("mode: 'live', turbine:","mode: 'backtest', turbine:").replace('API подключён · модель готова','Сохранённые прогнозы · офлайн').replace('Актуальная погода · прогноз от текущего момента','Офлайн-просмотр · показан сохранённый архивный прогноз февраля 2026 года').replace('Офлайн-просмотр: сохранённые данные и ответы по правилам, без LLM.','Офлайн-просмотр: сохранённые данные и ответы по правилам, без LLM.')
    html=(ROOT/'index.html').read_text(encoding='utf-8').replace('<link rel="stylesheet" href="styles.css">','<style>'+(ROOT/'styles.css').read_text(encoding='utf-8')+'</style>').replace('<script defer src="app.js"></script>','')
    for photo in ('wind-night.jpg', 'energy-grid.jpg', 'operator.jpg'):
        encoded = base64.b64encode((ROOT/'assets'/photo).read_bytes()).decode()
        html = html.replace('assets/'+photo, 'data:image/jpeg;base64,'+encoded)
    curve = base64.b64encode((ROOT/'assets'/'power_curves_turbines.png').read_bytes()).decode()
    html = html.replace('/assets/power_curves_turbines.png', 'data:image/png;base64,'+curve)
    html=html.replace('<body>','<body><div style="padding:12px 24px;background:#102b4b;color:#c0e8ff;font:13px/1.5 system-ui;text-align:center;position:relative;z-index:5">ОФЛАЙН-ПРОСМОТР · сохранённые архивные прогнозы февраля 2026 года. Live-погода, пересчёт и чат с LLM доступны после запуска Python-сервера.</div>')
    html=html.replace('</body>','<script>'+adapter+'</script><script>'+app+'</script></body>')
    (ROOT/'preview.html').write_text(html,encoding='utf-8')
    print(f'Exported {len(snapshots)} real forecast views to preview.html')
if __name__=='__main__':
    build()
