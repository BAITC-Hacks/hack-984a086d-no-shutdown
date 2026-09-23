# Wind Agent — прогноз выработки двух турбин

[**Презентация проекта**](https://canva.link/istoddntna5ut7e)

Локальное приложение объединяет подготовку телеметрии, ML-модель мощности, погодный API, проверяемый цикл агента и веб-панель. Оно формирует почасовой прогноз на 24 или 48 часов, сохраняет источник и время погодного выпуска, показывает предупреждения и выгружает результаты в CSV. Приложение рассчитано на демонстрацию и анализ; февральский прогноз пока нельзя считать подтверждённым оперативным прогнозом из-за ограничений архивной погоды и отсутствия фактической мощности за февраль.

## Быстрый запуск

На компьютере жюри нужен Python 3.12, добавленный в PATH; проверьте его в PowerShell командой `python --version`. Python 3.12 — проверенная версия, более новые версии пока не проверялись. Node.js не требуется.

**Windows (PowerShell):** откройте эту папку в VS Code и выполните:

```powershell
.\start.ps1
```

**macOS / Linux:**

```sh
bash start.sh
```

Скрипт создаёт `.venv` в папке проекта, устанавливает закреплённые зависимости из `requirements.lock.txt` и запускает локальный сервер. Откройте <http://127.0.0.1:8000>. Если PowerShell блокирует скрипт, выполните `powershell -ExecutionPolicy Bypass -File .\start.ps1` (только для этого процесса). Если порт занят, используйте `.\start.ps1 -Port 8001`. Остановка — Ctrl+C.

Модели уже входят в `artifacts/`; первый запуск не требует повторного обучения. Если предыдущая версия сервера запущена, остановите её перед запуском этой папки. Скрипт запуска не активирует `.venv` в новом терминале; ниже команды обучения и проверок вызывают Python из `.venv` явно.

Опциональный режим FastAPI с интерактивной схемой API доступен при установленной web-группе. Для обычного запуска на localhost он не требуется:

```powershell
& .\.venv\Scripts\python.exe -m pip install -e ".[web]"
& .\.venv\Scripts\python.exe -m windagent serve --fastapi
```

Документация API будет по адресу <http://127.0.0.1:8000/docs>. По умолчанию приложение использует стандартный HTTP-сервер Python.

Для быстрого просмотра интерфейса без установки Python дважды щёлкните `preview.html`. Этот режим показывает уже сохранённые архивные результаты; для live-погоды запустите сервер описанным выше способом.

## Что показать на демонстрации

1. На панели переключите T1/T2, исторический режим и горизонт 24/48 часов. Наведите курсор на график, выберите точку и скачайте CSV.
2. Откройте режим **«Сейчас · live»**. Он запрашивает погоду для следующего полного часа и использует ту же модель. В этой сборке выполнен свежий live-запрос на 48 ч для T1 и T2; проверка с 96 прогнозными строками и погодные снимки сохранены в [`reports/premium_live_proof.json`](reports/premium_live_proof.json) и `reports/premium_live_weather_turbine_1.json` / `reports/premium_live_weather_turbine_2.json`.
3. Спросите локального аналитика: «Сравни T1 и T2», «Найди лучшие три часа» или «Покажи резкие перепады». Ответ вычисляется из прогноза текущего запуска.
4. Покажите `reports/february_forecasts.csv` и `reports/february_replay.json`: это сохранённая реконструкция по 28 историческим срезам и 2 688 точкам, а не февральская оценка по измеренным фактам.

## Архитектура и технологии

```text
10-минутные CSV ──> проверка времени и качества ──> почасовые признаки
                                                     │
ECMWF / Open-Meteo ──> проверка доступности/кэша ────┤
                                                     v
                                      модели мощности T1 и T2
                                                     │
                                      агент / сервис / журнал SQLite
                                                     │
                                  HTTP API ──> HTML, CSS, JavaScript
```

- **Python 3, pandas, NumPy, scikit-learn, joblib**: очистка и агрегация телеметрии, временные признаки, обучение и инференс двух моделей HistGradientBoosting. Хеши входов и артефактов, настройки и метрики записаны в `artifacts/metadata.json`.
- **ECMWF IFS через Open-Meteo**: исторические одиночные погодные выпуски для реконструкции и запрос актуального прогноза для режима live. Погодные данные несут выпуск, время получения, координаты и хеш ответа.
- **Агент и сервис на Python**: проверяют горизонт и временной срез, получают погоду, вызывают модель, формируют предупреждения и пишут результаты/аудит в SQLite. Повторный запуск переиспользует неизменённые входы; изменившиеся входы запускают расчёт заново.
- **HTML, CSS, JavaScript и SVG**: адаптивная панель и график без npm-сборки и сторонних CDN.
- **Локальный чат** — аналитик на правилах, который использует контекст прогноза. Он не является LLM. Опциональный адаптер OpenAI Responses API включается через `.env`; ключ хранится только на сервере. Без ключа работает локальный аналитик.
- **HTTP**: компактный сервер Python включён по умолчанию; FastAPI/Uvicorn и `/docs` — дополнительный вариант.

## Данные и модель

Модели обучены на поставленных CSV с телеметрией T1/T2. Их контрольные суммы и качество данных приведены в `reports/dataset_profile.json`; конфигурация отбора и артефактные хеши — в `artifacts/metadata.json`. Используются только часы с шестью из шести десятиминутных измерений. Пропуски не превращаются в нулевую выработку. Данные доходят до 31 января 2026; фактических февральских измерений в наборе нет.

Кандидаты сравнивались на трёх последовательных временных окнах, январь оставался финальным holdout, а после оценки выбранная модель была переобучена на данных до 1 февраля. На январском holdout (744 часа на турбину), при подаче **фактического ветра и температуры**, получена MAE 0.0216948 для T1 и 0.0240447 для T2 в единицах нормализованной мощности. Это оценка условной зависимости мощности от известной погоды, а не точность прогноза на 24/48 часов. У T2 RMSE немного выше базового алгоритма; отчёт не заявляет улучшение каждой метрики. См. [`reports/training_report.md`](reports/training_report.md) и [`reports/supervisor_verification.json`](reports/supervisor_verification.json).

Архивная погодная реконструкция покрывает 28 февральских срезов. Провайдер помечает исторические ECMWF данные как hindcast; предполагаемая задержка публикации 12 часов не подтверждена первичным журналом доступности. Поэтому для сохранённых исторических расчётов `as_of_verified=false`: их нельзя выдавать за доказанный оперативный backtest. Свежий live-режим использует внешний API в момент запуска; отчёты интеграционных тестов с контролируемой фикстурой сами по себе не доказывают успешный сетевой вызов.

Номинальная мощность **2,5 МВт на турбину подтверждена пользователем проекта**. В наборе данных не найдена документация формулы нормализации; отображение нормализованной мощности в долю номинальной мощности поэтому является явным допущением. Значения MW/MWh в live-ответе помечаются как расчётные на этом допущении, а не как независимое измерение. Для этой демонстрационной сборки включён отдельный исследовательский cut-in/rated/cut-out сценарий. Его пороги остаются предположениями, а не спецификациями турбин; они меняют постобработку прогноза и не входят в январские метрики условной модели.

Часовой пояс `Asia/Almaty`, семантика timestamp `start` и нулевая задержка телеметрии — зафиксированные допущения, подлежащие сверке с владельцем данных.

## Воспроизведение и проверка

Для повторного обучения с тем же срезом и временным протоколом:

```powershell
& .\.venv\Scripts\python.exe -m windagent train --cutoff 2026-02-01T00:00:00+05:00 --timezone Asia/Almaty --timestamp-semantics start --min-samples 6 --validation-months 6 --folds 3
```

Чтобы создать свежий погодный прогноз на 48 часов и сохранить JSON/CSV:

```powershell
& .\.venv\Scripts\python.exe -m windagent live --horizon 48 --refresh --output reports/live_forecast.json --csv reports/live_forecast.csv
```

Для запуска автоматизированных проверок из корня проекта (без внешней сети):

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
& .\.venv\Scripts\python.exe scripts/verify_weather.py
& .\.venv\Scripts\python.exe scripts/verify_service.py --real
& .\.venv\Scripts\python.exe scripts/verify_delivery.py
& .\.venv\Scripts\python.exe scripts/verify_live.py
& .\.venv\Scripts\python.exe scripts/verify_live_http.py
& .\.venv\Scripts\python.exe scripts/verify_chat_tools.py
& .\.venv\Scripts\python.exe scripts/verify_model_revision.py
```

Финальная проверка: **88 passed, 21 subtests passed**; было одно предупреждение Starlette. В браузере проверены live T1/48 ч и T2/24 ч, архивный режим с предупреждением, ответ чата о лучшем трёхчасовом окне и четыре изображения; при 1280 px переполнения не было, ошибок и предупреждений консоли — 0. Свежий live-запрос для обеих турбин подтверждён в `reports/premium_live_proof.json`. Preview через браузер не проверялся: политика среды заблокировала локальный `file://` просмотр. Архивный hindcast всё ещё не доказывает историческую доступность; февральских фактов для оценки точности нет.

Полезные материалы: [инструкция для первого запуска](START_HERE.md), [карта критериев и сценарий защиты](docs/CRITERIA.md), [аудит премиальной сборки](docs/PREMIUM_REVIEW.md), [модель и временная валидация](docs/TUNING.md), [погода и ограничения архива](docs/WEATHER.md), [live-режим](docs/LIVE.md).

## Planned interactive features

The following features are planned enhancements, not implemented features of the current release.

### What if the wind changes?

A slider from **−30% to +30%** will let users adjust wind speed and rerun the power model. The chart will show the original forecast alongside the scenario forecast. The adjusted output must carry a prominent **“Scenario”** label so it cannot be confused with an actual weather forecast.

### Live agent workflow

Clicking **“Recalculate”** will highlight the stages **Weather → Data validation → Model → Analysis → Result**. Each stage will expand to show its data source, execution time, and any issues detected. Status updates must come from the actual Python execution, rather than a simulated animation.

### Turbine battle: T1 vs T2

Two synchronized charts will compare the turbines’ peak output and power stability. The view will highlight hours when one turbine produces more than the other on the **normalized power scale**, with an **“Explain the difference”** button beside the comparison.
