# Обучение модели мощности

Версия `conditional-power-v2`. Данные ограничены `2026-02-01T00:00:00+05:00`. Измерений в час: минимум 6/6.

## Проверка без перемешивания времени

Выбор модели: 3 последовательных окон между 2025-07-01 и 2026-01-01. Каждое окно использует только предшествующие наблюдения. Критерий — среднее MAE окон.

Финальная проверка: 2026-01-01 — 2026-02-01 (правая граница исключена). Этот период не используется для выбора модели. После оценки выбранная модель переобучена на всех доступных данных для рабочего прогноза.

| Турбина | Часов обучения | Выбранная модель | CV MAE | Holdout MAE | Исходный HGB MAE | Holdout RMSE |
|---|---:|---|---:|---:|---:|---:|
| T1 | 23,666 | hgb_mae_smooth | 0.01657 | 0.02169 | 0.02362 | 0.04805 |
| T2 | 24,784 | hgb_mae_weather_only | 0.01668 | 0.02404 | 0.02501 | 0.06413 |

MAE/RMSE — в единицах нормализованной мощности. Это ошибки при известных фактических ветре и температуре; они НЕ измеряют точность прогноза на 24–48 часов. Оба алгоритма сравниваются на одинаковых полных часах и временных границах.

T2: MAE 0.02404 против 0.02501 у исходного HGB; RMSE немного хуже (0.06413 против 0.06264). Критерием выбора был MAE; улучшение по всем метрикам не заявляется.

Все кандидаты и метрики окон: `artifacts/metadata.json`. Индивидуальные прогнозы: `reports/conditional_holdout_predictions.csv`. Качество CSV: `reports/dataset_profile.json`.

## Интервал и ограничения

Полоса — 90-й перцентиль абсолютных ошибок выбранной модели на скользящих окнах; это условный разброс ошибки мощности при заданной погоде. Он не включает неопределённость будущей погоды и не гарантирует 90% покрытия будущих наблюдений.

- No February 2026 actual turbine power was supplied; February accuracy cannot be measured.
- Scores use observed weather, not forecast weather; they are conditional power-model diagnostics, not 24–48 h forecast skill.
- Residual bands exclude weather uncertainty and have no guaranteed coverage for operational forecasts.
- CSV timezone=Asia/Almaty, timestamp=start, telemetry delay=0 are explicit unconfirmed assumptions.
- Power normalization formula and rated capacities are unknown; predictions are dimensionless, not MW/MWh.

Настройка и воспроизведение: `docs/TUNING.md`. random_state=17 и SHA-256 исходных данных/артефактов записаны в метаданных.
