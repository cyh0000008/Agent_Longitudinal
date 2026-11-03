import io
import json
import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from openpyxl import Workbook

import kpi_report_analysis as kpi
import llm_service
from kpi_report_charts import Metric, ReportChart, extract_chart_reports, render_dashboard, number, significant, excluded_metric


class KpiReportAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.config = kpi.KpiReportConfig(base_token="base", table_id="table")
        self.kimi_config = llm_service.LLMConfig(
            "https://api.kimi.com/coding/v1", "synthetic-key", "kimi-for-coding", 120,
        )
        config_patch = patch.object(kpi, "get_kimi_config", return_value=self.kimi_config)
        config_patch.start()
        self.addCleanup(config_patch.stop)

    def test_kimi_config_does_not_inherit_general_model_settings(self):
        with patch.object(llm_service, "load_env"), patch.dict(os.environ, {
            "KIMI_API_KEY": "synthetic-key",
            "KIMI_MODEL": "",
            "KIMI_TIMEOUT_SECONDS": "120",
            "LLM_API_BASE_URL": "https://unrelated.invalid/v1",
            "LLM_MODEL": "unrelated-model",
        }):
            config = llm_service.get_kimi_config()
        self.assertEqual(config, self.kimi_config)

    def test_missing_kimi_key_never_falls_back(self):
        with patch.object(llm_service, "load_env"), patch.dict(os.environ, {
            "KIMI_API_KEY": "", "LLM_API_KEY": "unrelated-key",
        }), self.assertRaisesRegex(RuntimeError, "KIMI_API_KEY"):
            llm_service.get_kimi_config()

    def test_preflight_uses_kimi_timeout_and_reports_failure(self):
        with patch.object(llm_service, "call_chat_completion", return_value="聊天模型调用失败：timed out") as chat:
            ok, detail = llm_service.probe_llm_api_available(
                config=self.kimi_config, api_protocol="openai",
            )
        self.assertFalse(ok)
        self.assertIn("timed out", detail)
        self.assertEqual(chat.call_args.kwargs["timeout_seconds"], 120)

    def test_preflight_explicit_timeout_takes_precedence(self):
        with patch.object(llm_service, "call_chat_completion", return_value="OK") as chat:
            ok, _ = llm_service.probe_llm_api_available(
                config=self.kimi_config, api_protocol="openai", timeout_seconds=30,
            )
        self.assertTrue(ok)
        self.assertEqual(chat.call_args.kwargs["timeout_seconds"], 30)

    def test_kimi_request_ignores_global_anthropic_protocol(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": "OK"}}],
        }).encode()
        with patch.dict(os.environ, {"LLM_API_PROTOCOL": "anthropic"}), \
             patch.object(llm_service.request, "urlopen", return_value=response) as urlopen, \
             patch.object(llm_service, "get_llm_config") as general_config:
            reply = llm_service.call_chat_completion(
                [{"role": "user", "content": "hello"}],
                config=self.kimi_config, api_protocol="openai",
            )
        general_config.assert_not_called()
        req = urlopen.call_args.args[0]
        self.assertEqual(req.full_url, "https://api.kimi.com/coding/v1/chat/completions")
        self.assertEqual(json.loads(req.data)["model"], "kimi-for-coding")
        self.assertEqual(reply, "OK")

    def test_chunk_sends_only_extracted_report_content(self):
        captured = {}

        def fake_chat(messages, **kwargs):
            captured["prompt"] = messages[-1]["content"]
            self.assertEqual(kwargs["config"], self.kimi_config)
            self.assertEqual(kwargs["api_protocol"], "openai")
            return json.dumps({"rows": [{
                "record_id": "rec1",
                "analysis": "Target 的跟车起步响应时延 CPI 下降。",
                "causes": "报告仅提供KPI结果，未提供足够的问题原因证据。",
                "actions": "核查跟车起步场景。",
            }]}, ensure_ascii=False)

        with patch.object(kpi, "call_chat_completion", side_effect=fake_chat):
            rows = kpi._analyze_report_chunk([("rec1", 1, "第22行：B=跟车起步响应时延 | F=14.0 | H=6.8")])

        self.assertEqual(rows[0].status, "success")
        self.assertIn("跟车起步响应时延", captured["prompt"])
        self.assertNotIn("张三", captured["prompt"])
        self.assertIn("Bad Case列表", captured["prompt"])

    def test_run_uses_real_fields_and_resets_trigger_after_success(self):
        records = [
            {"record_id": "rec1", "fields": {
                "版本KPI报告": [{"file_token": "f1", "name": "report.xlsx"}],
                "是否进行KPI报告AI分析": "是",
                "Bad Case列表": [{"file_token": "must-not-read"}],
            }},
            {"record_id": "rec2", "fields": {
                "版本KPI报告": [{"file_token": "f2", "name": "report.xlsx"}],
                "是否进行KPI报告AI分析": "否",
            }},
        ]
        writes = []
        analyzed = kpi.KpiRowAnalysis("rec1", 1, "摘要", "无原因证据", "建议")
        with patch.object(kpi, "_list_field_names", return_value=set(self.config.required_fields)), \
             patch.object(kpi, "_list_records", return_value=records), \
             patch.object(kpi, "probe_llm_api_available", return_value=(True, "OK")) as probe, \
             patch.object(kpi, "_extract_record_report", return_value="附件提取内容") as extract, \
             patch.object(kpi, "_analyze_report_chunk", return_value=[analyzed]) as analyze, \
             patch.object(kpi, "render_dashboard"), \
             patch.object(kpi, "_tenant_access_token", return_value="test-token"), \
             patch.object(kpi, "_upload_bitable_media", return_value="test-image"), \
             patch.object(kpi, "_update_record", side_effect=lambda _, rid, fields: writes.append((rid, fields))):
            result = kpi.run_kpi_report_analysis(config=self.config)

        self.assertEqual(result.pending_count, 1)
        probe.assert_called_once_with(config=self.kimi_config, api_protocol="openai")
        self.assertEqual(result.success_count, 1)
        extract.assert_called_once()
        self.assertEqual(analyze.call_args.args[0], [("rec1", 1, "附件提取内容")])
        self.assertEqual(writes[0][1]["是否进行KPI报告AI分析"], "否")
        self.assertIn("【KPI分析摘要】", writes[0][1]["KPI报告AI分析结果"])
        self.assertNotIn("Bad Case列表", writes[0][1])
        self.assertEqual(writes[0][1][self.config.image_field], [{"file_token": "test-image"}])
        self.assertNotIn("【问题原因】", writes[0][1][self.config.result_field])

    def test_numeric_summary_is_rejected(self):
        reply = json.dumps({"rows": [{"record_id": "rec1", "analysis": "CPI下降到10"}]})
        with patch.object(kpi, "call_chat_completion", return_value=reply):
            rows = kpi._analyze_report_chunk([("rec1", 1, "synthetic")])
        self.assertEqual(rows[0].status, "failed")

    def test_compact_reduces_repetition_without_removing_evidence(self):
        repeated = "a long shared criterion for this fictional repeated metric"
        content = "\n".join(f"第{i}行：B=metric{i} | D={repeated} | F=20 | H=10" for i in range(20))
        compact = kpi._compact_report_context(content)
        self.assertLess(len(compact), len(content) * 0.7)
        self.assertIn(repeated, compact)
        for i in range(20):
            self.assertIn(f"{i}:B=metric{i}", compact)
        self.assertEqual(compact.count("F=20|H=10"), 20)

    def test_uniform_summary_image(self):
        from PIL import Image
        report = ReportChart("synthetic", "Ver1", "Ver2", [
            Metric("合成测试指标", "CPI", "20(100/5)", "10(100/10)", "CPI ≥15", "可准出", "不满足准出", "Sheet!H7"),
        ])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "summary.png"
            render_dashboard([report], path)
            with Image.open(path) as image:
                self.assertEqual(image.width, 1600)
                self.assertGreater(image.height, 700)
                self.assertEqual(image.format, "PNG")

    def chart_workbook(self, baseline="Ver1", green=True):
        from openpyxl.styles import PatternFill
        workbook = Workbook()
        sheet = workbook.active
        sheet["A1"], sheet["E1"], sheet["G1"] = "车型", "TestCar", "TestCar"
        sheet["A2"], sheet["E2"], sheet["G2"] = "动力形式", "ICE", "ICE"
        sheet["A3"], sheet["E3"], sheet["G3"] = "日期", "20261001-20261002", "20261021-20261022"
        sheet["A4"], sheet["E4"], sheet["G4"] = "软件版本", baseline, "Ver2"
        sheet.merge_cells("E4:F4")
        sheet.merge_cells("G4:H4")
        if green:
            sheet["G4"].fill = PatternFill("solid", fgColor="75BD42")
        for col, value in zip(("E5", "F5", "G5", "H5"), ("MPI", "CPI", "MPI", "CPI")):
            sheet[col] = value
        for col, value in zip(("B6", "I6", "J6", "K6", "L6"),
                              ("指标名称", "Base准出标准", "Base准出结论", "Target准出标准", "Target准出结论")):
            sheet[col] = value
        sheet.append([None, "舒适性", None, None, "\\", "20(100/5)", "\\", "10(100/10)",
                      "CPI ≥15", "可准出", "CPI ≥15", "不满足准出"])
        sheet.append([None, "事件", None, None, ">900(900/0)", "\\", ">800(800/0)", "\\",
                      "MPI ≥500", "可准出", "MPI ≥500", "可准出"])
        buffer = io.BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()

    def test_chart_extracts_green_version_and_does_not_compare_bounds(self):
        reports = extract_chart_reports("synthetic.xlsx", self.chart_workbook())
        self.assertEqual(reports[0].current_version, "Ver2")
        self.assertEqual(reports[0].previous_version, "Ver1")
        self.assertEqual([m.direction for m in reports[0].metrics], ["退化", "不可比"])
        self.assertEqual(reports[0].metrics[0].current, "10(100/10)")
        self.assertEqual(reports[0].metrics[0].transition, "新增未准出")
        self.assertEqual(reports[0].vehicle, "TestCar")
        self.assertEqual(reports[0].powertrain, "ICE")
        self.assertEqual(reports[0].previous_dates, "20261001-20261002")
        self.assertEqual(reports[0].current_dates, "20261021-20261022")

    def test_dashboard_hides_file_source_and_incomparable_labels(self):
        from PIL import ImageDraw
        report = extract_chart_reports("private-file.xlsx", self.chart_workbook())[0]
        bound = Metric("bound", "MPI", ">100(100/0)", ">90(90/0)", "MPI ≥95",
                       "可准出", "不满足准出", "Sheet1!H11")
        report.metrics.append(bound)
        texts = []
        original = ImageDraw.ImageDraw.text

        def capture(draw, xy, text, *args, **kwargs):
            texts.append(str(text))
            return original(draw, xy, text, *args, **kwargs)

        with tempfile.TemporaryDirectory() as folder, patch.object(ImageDraw.ImageDraw, "text", new=capture):
            render_dashboard([report], Path(folder) / "chart.png")
        rendered = "\n".join(texts)
        for forbidden in ("private-file", "Sheet", "来源", "不可比"):
            self.assertNotIn(forbidden, rendered)
        for expected in ("TestCar", "ICE", "20261021-20261022", "Ver1", "Ver2", "新增未准出"):
            self.assertIn(expected, rendered)

    def test_zero_event_and_missing_values_are_not_numeric_improvements(self):
        self.assertIsNone(number("100.0(10/0)"))
        self.assertIsNone(number(">900(900/0)"))
        self.assertIsNone(number("\\"))
        self.assertEqual(number("0(100/100)"), 0)

    def test_significant_change_exact_boundaries(self):
        def metric(before, after, old="可准出", new="可准出"):
            return Metric("test", "CPI", before, after, "", old, new, "Sheet!H7")
        self.assertFalse(significant(metric("12", "18")))
        self.assertTrue(significant(metric("14", "21")))
        self.assertFalse(significant(metric("100", "149")))
        self.assertTrue(significant(metric("100", "50")))
        self.assertFalse(significant(metric("0", "10")))
        self.assertTrue(significant(metric(">100(100/0)", ">90(90/0)", new="不满足准出")))
        self.assertFalse(significant(metric(">100(100/0)", ">90(90/0)")))

    def test_excludes_only_abs_tcs_vse_acc_exit_metrics(self):
        for system in ("ABS", "TCS", "VSE"):
            self.assertTrue(excluded_metric(f"{system}触发关联ACC退出"))
        self.assertFalse(excluded_metric("其他ACC退出"))
        self.assertFalse(excluded_metric("ABS制动"))

    def test_chart_comparison_filter_preserves_overview(self):
        report = extract_chart_reports("synthetic.xlsx", self.chart_workbook())[0]
        self.assertEqual(len(report.metrics), 2)
        self.assertEqual(len(report.comparisons), 1)
        no_base = extract_chart_reports("synthetic.xlsx", self.chart_workbook("无基线"))[0]
        self.assertEqual(len(no_base.comparisons), 1)
        self.assertEqual(no_base.comparisons[0].name, "舒适性")

    def test_theme_green_header_is_supported(self):
        from openpyxl import load_workbook
        from openpyxl.styles import Color, PatternFill
        workbook = load_workbook(io.BytesIO(self.chart_workbook()))
        workbook.active["G4"].fill = PatternFill("solid", fgColor=Color(theme=6))
        buffer = io.BytesIO()
        workbook.save(buffer)
        reports = extract_chart_reports("synthetic.xlsx", buffer.getvalue())
        self.assertEqual(reports[0].current_version, "Ver2")

    def test_chart_without_baseline_never_reports_improvement(self):
        report = extract_chart_reports("synthetic.xlsx", self.chart_workbook("无基线"))[0]
        self.assertEqual(report.previous_version, "")
        self.assertTrue(all(m.direction == "不可比" and not m.previous for m in report.metrics))

    def test_chart_missing_green_header_is_explicit_error(self):
        with self.assertRaisesRegex(ValueError, "绿色当前版本"):
            extract_chart_reports("synthetic.xlsx", self.chart_workbook(green=False))

    def test_pipeline_charts_use_workbook_not_ai_summary(self):
        from PIL import Image
        records = [{"record_id": "rec1", "fields": {
            self.config.trigger_field: "是",
            self.config.report_field: [{"file_token": "synthetic", "name": "synthetic.xlsx"}],
        }}]
        writes = []

        def upload(token, base, path):
            with Image.open(path) as image:
                self.assertEqual(image.width, 1600)
                self.assertEqual(image.height, 886)
            return "chart-token"

        with patch.object(kpi, "_list_field_names", return_value=set(self.config.required_fields)), \
             patch.object(kpi, "_list_records", return_value=records), \
             patch.object(kpi, "probe_llm_api_available", return_value=(True, "")), \
             patch.object(kpi, "_download_attachment", return_value=self.chart_workbook()), \
             patch.object(kpi, "_analyze_report_chunk", return_value=[kpi.KpiRowAnalysis("rec1", 1, "定性摘要", "", "")]), \
             patch.object(kpi, "_tenant_access_token", return_value="token"), \
             patch.object(kpi, "_upload_bitable_media", side_effect=upload), \
             patch.object(kpi, "_update_record", side_effect=lambda c, r, f: writes.append(f)):
            result = kpi.run_kpi_report_analysis(config=self.config)
        self.assertEqual(result.success_count, 1)
        self.assertEqual(writes[0][self.config.image_field], [{"file_token": "chart-token"}])

    def test_image_upload_failure_does_not_write_summary_or_reset_trigger(self):
        records = [{"record_id": "rec1", "fields": {
            "是否进行KPI报告AI分析": "是", "版本KPI报告": [{"file_token": "f"}],
        }}]
        analyzed = kpi.KpiRowAnalysis("rec1", 1, "摘要", "", "")
        with patch.object(kpi, "_list_field_names", return_value=set(self.config.required_fields)), \
             patch.object(kpi, "_list_records", return_value=records), \
             patch.object(kpi, "probe_llm_api_available", return_value=(True, "")), \
             patch.object(kpi, "_extract_record_report", return_value="synthetic"), \
             patch.object(kpi, "_analyze_report_chunk", return_value=[analyzed]), \
             patch.object(kpi, "render_dashboard") as render, \
             patch.object(kpi, "_tenant_access_token", return_value="test"), \
             patch.object(kpi, "_upload_bitable_media", side_effect=RuntimeError("upload denied")), \
             patch.object(kpi, "_update_record") as update:
            result = kpi.run_kpi_report_analysis(config=self.config)
        update.assert_not_called()
        self.assertEqual(result.success_count, 0)
        self.assertEqual(result.failed_count, 1)
        self.assertFalse(render.call_args.args[1].parent.exists())

    def test_failure_keeps_trigger_and_record_unchanged(self):
        records = [{"record_id": "rec1", "fields": {
            "版本KPI报告": [], "是否进行KPI报告AI分析": "是",
        }}]
        with patch.object(kpi, "_list_field_names", return_value=set(self.config.required_fields)), \
             patch.object(kpi, "_list_records", return_value=records), \
             patch.object(kpi, "probe_llm_api_available", return_value=(True, "OK")), \
             patch.object(kpi, "_update_record") as update:
            result = kpi.run_kpi_report_analysis(config=self.config)

        self.assertEqual(result.failed_count, 1)
        self.assertIn("附件列", result.rows[0].error)
        update.assert_not_called()

    def test_llm_preflight_failure_does_not_download_or_modify(self):
        records = [{"record_id": "rec1", "fields": {
            "版本KPI报告": [{"file_token": "f1", "name": "report.xlsx"}],
            "是否进行KPI报告AI分析": "是",
        }}]
        with patch.object(kpi, "_list_field_names", return_value=set(self.config.required_fields)), \
             patch.object(kpi, "_list_records", return_value=records), \
             patch.object(kpi, "probe_llm_api_available", return_value=(False, "API不可用")), \
             patch.object(kpi, "_download_attachment") as download, \
             patch.object(kpi, "_update_record") as update, \
             self.assertRaisesRegex(RuntimeError, "多维表格未修改"):
            kpi.run_kpi_report_analysis(config=self.config)
        download.assert_not_called()
        update.assert_not_called()

    def test_excel_attachment_is_converted_to_traceable_rows(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "KPI"
        sheet["A1"] = "指标"
        sheet["B1"] = "Base CPI"
        sheet["C1"] = "Target CPI"
        sheet["A2"] = "跟车起步响应时延"
        sheet["B2"] = 14.0
        sheet["C2"] = 6.8
        buffer = io.BytesIO()
        workbook.save(buffer)

        text = kpi._extract_attachment_text("report.xlsx", buffer.getvalue(), 10_000)

        self.assertIn("工作表：KPI", text)
        self.assertIn("第2行", text)
        self.assertIn("A=跟车起步响应时延", text)
        self.assertIn("B=14", text)
        self.assertIn("C=6.8", text)


if __name__ == "__main__":
    unittest.main()
