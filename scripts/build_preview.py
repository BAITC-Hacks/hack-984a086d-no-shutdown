"""Export real stored predictions into a portable, explicitly offline HTML viewer."""
from __future__ import annotations
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from windagent import service
from windagent.chat import _local_reply

def build():
    metadata = service.status()
    snapshots = {}
    questions = {'accuracy':'Какая точность модели?', 'training':'Как настроить модель?', 'asof':'Есть ли утечка?', 'weather':'Какой ветер?', 'summary':'Объясни прогноз', 'other':'Помоги с другим вопросом'}
    for day in range(1,29):
        date = f'2026-02-{day:02d}'
        for turbine in ('T1','T2'):
            complete = service.dashboard_forecast(turbine, date, 48)
            for horizon in (24,48):
                data = {**complete, 'forecast':complete['forecast'][:horizon], 'horizon_hours':horizon}
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
    if(!snapshot){code=422;value={detail:'В офлайн-просмотре доступны сохранённые прогнозы с 1 по 28 февраля. Для другого среза запустите Python-сервер.'};}
    else if(input.refresh==='true'){code=422;value={detail:'Офлайн-просмотр не запускает модель заново. Для пересчёта откройте основной сайт через start.sh / start.ps1.'};}
    else if(address.pathname==='/api/forecast') value=snapshot.forecast;
    else if(address.pathname==='/api/chat') {
      const query=(input.message||'').toLowerCase();
      const groups=[['accuracy',['точност','ошиб','mae','метрик','качеств']],['training',['обуч','настро','параметр','улучш']],['asof',['утеч','будущ','as_of','архив','предупреж','огранич']],['weather',['ветер','погод','температур']],['summary',['прогноз','пик','мощност','сводк','энерги']]];
      const topic=groups.find(([,words])=>words.some(word=>query.includes(word)))?.[0]||'other';
      value={reply:snapshot.answers[topic],mode:'local',actions:[],context:{offline:true}};
    } else {code=404;value={detail:'Offline endpoint not available'};}
  }
  return new Response(JSON.stringify(value),{status:code,headers:{'Content-Type':'application/json'}});
};
'''.replace('__DATA__',payload)
    app=(ROOT/'app.js').read_text().replace("if (location.protocol === 'file:')",'if (false)').replace('API подключён · модель готова','Сохранённые прогнозы · офлайн').replace('Локальный режим: анализ данных по правилам, без LLM.','Офлайн-просмотр: сохранённые данные и ответы по правилам, без LLM.')
    html=(ROOT/'index.html').read_text().replace('<link rel="stylesheet" href="styles.css">','<style>'+(ROOT/'styles.css').read_text()+'</style>').replace('<script defer src="app.js"></script>','')
    html=html.replace('<body>','<body><div style="padding:12px 24px;background:#102b4b;color:#c0e8ff;font:13px/1.5 system-ui;text-align:center;position:relative;z-index:5">ОФЛАЙН-ПРОСМОТР · реальные сохранённые прогнозы обученной модели. Пересчёт и подключение LLM — в основном сайте после запуска Python-сервера.</div>')
    html=html.replace('</body>','<script>'+adapter+'</script><script>'+app+'</script></body>')
    (ROOT/'preview.html').write_text(html,encoding='utf-8')
    print(f'Exported {len(snapshots)} real forecast views to preview.html')
if __name__=='__main__':
    build()
