"""Known-number checks for analyst tools; no network, model fit or API key."""
import copy
import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from windagent import chat, service


def fixture(values=None, *, start="2026-01-31T19:00:00+00:00", mode="backtest", zone="Asia/Almaty"):
    values = values if values is not None else [0.2] * 24
    first = datetime.fromisoformat(start)
    rows = [{"timestamp": (first + timedelta(hours=index)).isoformat(),
             "predicted_power": power, "lower": max(0, power - .1), "upper": min(1, power + .1),
             "wind_speed": 4 + index, "temperature": index - 10}
            for index, power in enumerate(values)]
    return {"forecast": rows, "date_timezone": zone, "turbine_id": "T1",
            "as_of_date": "2026-02-01", "origin": start, "mode": mode,
            "warnings": ["normalized units", "Проверочная граница модели"],
            "horizon_hours": len(rows), "forecast_id": 42, "as_of_verified": False,
            "power_unit": "normalized", "capacity_mw": None}


class ChatToolTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"WINDAGENT_CHAT_PROVIDER": "local"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_best_window_has_exclusive_end_and_site_timezone(self):
        data = fixture([.1, .2, .8, .9, .7] + [.1] * 19)
        reply = chat._local_reply("Найди лучшее окно из 3 часов", data, {})
        self.assertIn("01.02 в 02:00", reply)
        self.assertIn("01.02 в 05:00", reply)
        self.assertIn("конец не включён", reply)
        self.assertIn("0.800", reply)
        self.assertIn("7.0 м/с", reply)
        self.assertIn("22 окон", reply)

    def test_best_window_tie_uses_first_and_keeps_input_unchanged(self):
        data = fixture([.5] * 24)
        before = copy.deepcopy(data)
        reply = chat._local_reply("Лучшее окно", data, {})
        self.assertIn("01.02 в 00:00", reply)
        self.assertIn("01.02 в 03:00", reply)
        self.assertEqual(data, before)

    def test_ramp_sign_and_timestamps(self):
        data = fixture([.1, .2, .9, .8] + [.8] * 20)
        reply = chat._local_reply("Найди самый резкий скачок", data, {})
        self.assertIn("увеличение", reply)
        self.assertIn("+0.700", reply)
        self.assertIn("0.200 → 0.900", reply)
        self.assertIn("01.02 в 01:00", reply)
        self.assertIn("01.02 в 02:00", reply)
        decreasing = fixture([.9, .2] + [.2] * 22)
        reply = chat._local_reply("ramp", decreasing, {})
        self.assertIn("снижение", reply)
        self.assertIn("-0.700", reply)

    def test_calendar_days_show_partial_counts_and_correct_means(self):
        data = fixture([.1] * 2 + [.4] * 24 + [.9] * 22, start="2026-02-01T17:00:00+00:00", mode="live")
        reply = chat._local_reply("Сравни сегодня и завтра", data, {})
        self.assertIn("2026-02-01: 2 ч, средняя мощность 0.100", reply)
        self.assertIn("2026-02-02: 24 ч, средняя мощность 0.400", reply)
        self.assertIn("2026-02-03: 22 ч, средняя мощность 0.900", reply)
        self.assertIn("не являются энергией за сутки", reply)

    def test_calendar_day_label_uses_declared_timezone(self):
        data = fixture([.2] * 24, start="2026-02-01T22:00:00+00:00", zone="Europe/Berlin")
        reply = chat._local_reply("Сравни календарные дни сегодня и завтра", data, {})
        self.assertIn("Europe/Berlin", reply)
        self.assertNotIn("Asia/Almaty", reply)
        self.assertIn("2026-02-01: 1 ч", reply)
        self.assertIn("2026-02-02: 23 ч", reply)

    def test_first_and_second_24h_are_horizon_blocks_not_calendar_days(self):
        data = fixture([.1] * 2 + [.4] * 24 + [.9] * 22, start="2026-02-01T17:00:00+00:00", mode="live")
        reply = chat._local_reply("Сравни первые 24 и вторые 24 часа", data, {})
        # First block: (2*.1 + 22*.4)/24 = .375. Second: (2*.4+22*.9)/24 = .858333.
        self.assertIn("0.375", reply)
        self.assertIn("0.858", reply)
        self.assertNotIn("2026-02-01: 2 ч", reply)

    def test_24h_forecast_does_not_invent_a_second_block(self):
        data = fixture([.2] * 24, mode="live")
        reply = chat._local_reply("Сравни первые 24 и вторые 24 часа", data, {})
        self.assertIn("48", reply)
        self.assertTrue(any(word in reply.casefold() for word in ("выберите", "нужен", "нужны", "включите")))
        self.assertNotIn("0.000", reply)

    def test_fleet_summary_and_comparison_use_normalized_power(self):
        series = {"1": [{"timestamp": "2026-01-31T19:00:00Z", "power": .1}, {"timestamp": "2026-01-31T20:00:00Z", "power": .9}],
                  "2": [{"timestamp": "2026-01-31T19:00:00Z", "power": .2}, {"timestamp": "2026-01-31T20:00:00Z", "power": .4}]}
        fleet = service.fleet_summary(series)
        self.assertAlmostEqual(fleet["T1"]["mean"], .5)
        self.assertAlmostEqual(fleet["T2"]["mean"], .3)
        self.assertEqual(fleet["T1"]["peak_at"], "2026-01-31T20:00:00Z")
        data = fixture()
        data["fleet_summary"] = fleet
        reply = chat._local_reply("Сравни турбины T1 и T2", data, {})
        self.assertIn("T1 выше на 0.200", reply)
        self.assertIn("норм. единицы", reply)
        self.assertIn("не доказательство", reply)
        self.assertIn("нормализация", reply)

    def test_live_and_historical_audits_are_distinct(self):
        live = chat._local_reply("Проверь данные и предупреждения", fixture(mode="live"), {})
        historical = chat._local_reply("Проверь данные и предупреждения", fixture(), {})
        self.assertIn("свежий прогноз", live)
        self.assertIn("не измерения SCADA", live)
        self.assertNotIn("run + 12", live)
        self.assertNotIn("as_of_verified=false", live)
        self.assertIn("as_of_verified=false", historical)
        self.assertIn("WINDAGENT_STRICT_AS_OF", historical)
        summary = chat._local_reply("Сводка прогноза", fixture(mode="live"), {})
        self.assertNotIn("Историческая доступность погодного архива", summary)

    def test_invalid_mode_rejected_before_any_agent_call(self):
        unused = Mock()
        for mode in ("LIVE", "historical", "unknown", None, 123, []):
            with self.subTest(mode=mode), self.assertRaises(service.ServiceError):
                service.dashboard_forecast("T1", "2026-02-01", 24, agent=unused, mode=mode)
        unused.run.assert_not_called()
        unused._metadata.assert_not_called()

    def test_answer_passes_selected_mode_and_context_to_forecast(self):
        agent = SimpleNamespace(_metadata=lambda: {})
        with patch.object(chat, "dashboard_forecast", return_value=fixture(mode="live")) as forecast:
            reply = chat.answer({"message": "Сводка прогноза", "mode": "live", "horizon_hours": 24}, agent=agent)
        self.assertEqual(forecast.call_args.kwargs["mode"], "live")
        self.assertEqual(reply["context"]["mode"], "live")
        self.assertEqual(reply["mode"], "local")
        self.assertNotIn("Историческая доступность погодного архива", reply["reply"])

    def test_live_mode_calls_only_live_agent(self):
        historical = Mock()
        live = Mock()
        raw = {"id": 42}
        live.run.return_value = raw
        expected = fixture(mode="live")
        with patch.object(service, "get_live_agent", return_value=live), patch.object(service, "live_dashboard", return_value=expected) as project:
            result = service.dashboard_forecast("T2", "date-is-irrelevant-in-live-mode", "24", "false", agent=historical, mode="live")
        live.run.assert_called_once_with(24, False)
        historical.run.assert_not_called()
        historical._metadata.assert_not_called()
        project.assert_called_once_with("T2", raw)
        self.assertIs(result, expected)

    def test_live_dashboard_ignores_unconfirmed_mw_values(self):
        points = []
        for row in fixture([.2] * 24)["forecast"]:
            points.append({"timestamp": row["timestamp"], "normalized_power": .2,
                           "lower_normalized": .1, "upper_normalized": .3,
                           "power_mw": 99, "energy_mwh": 99, "wind_speed": 7, "temperature": -2})
        result = {"id": 88, "forecast_start": "2026-01-31T19:00:00Z", "issued_at": "2026-01-31T18:50:00Z",
                  "checked_at": "2026-01-31T18:51:00Z", "model": {"model_version": "fixture"},
                  "turbines": [{"turbine_id": 1, "points": points, "current": {},
                                "provenance": {"source": "fixture weather", "retrieved_at": "2026-01-31T18:50:00Z"}},
                               {"turbine_id": 2, "points": [dict(p, normalized_power=.8) for p in points], "current": {},
                                "provenance": {"source": "fixture weather", "retrieved_at": "2026-01-31T18:50:00Z"}}]}
        data = service.live_dashboard("T1", result)
        self.assertEqual(data["mode"], "live")
        self.assertEqual(data["as_of_date"], "2026-02-01")
        self.assertEqual(data["forecast"][0]["predicted_power"], .2)
        self.assertAlmostEqual(data["fleet_summary"]["T2"]["mean"], .8)
        self.assertEqual(data["power_unit"], "normalized")
        self.assertIsNone(data["capacity_mw"])
        self.assertNotIn("power_mw", data["forecast"][0])
        self.assertFalse(any("hindcast" in w.casefold() for w in data["warnings"]))


if __name__ == "__main__":
    unittest.main()
