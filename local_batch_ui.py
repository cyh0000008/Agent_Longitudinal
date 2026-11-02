from __future__ import annotations

import argparse
import cgi
import html
import json
import mimetypes
import os
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from local_batch_root_cause import (
    load_local_batch_config,
    run_local_excel_batch_analysis,
)
from runtime_paths import ensure_writable_dir, get_runtime_subdir, safe_runtime_name


@dataclass
class BatchJob:
    job_id: str
    status: str
    stage: str
    detail: str
    input_name: str
    created_at: float
    updated_at: float
    output_zip: str = ""
    output_excel: str = ""
    error: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    history: list[dict[str, str]] = field(default_factory=list)


JOBS: dict[str, BatchJob] = {}
JOBS_LOCK = threading.RLock()


def get_ui_runtime_dir() -> Path:
    return get_runtime_subdir("local_batch_ui")


def create_handler(base_dir: Path):
    class LocalBatchHandler(BaseHTTPRequestHandler):
        server_version = "BluLocalBatch/1.0"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if path == "/":
                self._send_html(INDEX_HTML)
                return
            if path == "/healthz":
                self._send_json({"ok": True})
                return
            if path.startswith("/api/jobs/"):
                parts = [item for item in path.split("/") if item]
                if len(parts) == 3:
                    self._send_job(parts[2])
                    return
                if len(parts) == 4 and parts[3] == "download":
                    self._send_download(parts[2])
                    return
            self._send_json({"ok": False, "error": "not_found"}, status=404)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path.rstrip("/") != "/api/jobs":
                self._send_json({"ok": False, "error": "not_found"}, status=404)
                return
            try:
                job = self._create_job_from_upload()
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
                return
            self._send_json({"ok": True, "job": serialize_job(job)})

        def log_message(self, fmt: str, *args) -> None:
            print(f"[local-batch-ui] {self.address_string()} - {fmt % args}", flush=True)

        def _create_job_from_upload(self) -> BatchJob:
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type:
                raise ValueError("请使用 multipart/form-data 上传 Excel。")

            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": content_type,
                },
            )
            file_item = form["excel"] if "excel" in form else None
            if file_item is None or not getattr(file_item, "filename", ""):
                raise ValueError("缺少 Excel 文件。")
            file_name = safe_runtime_name(Path(file_item.filename).name, "input.xlsx", max_length=120)
            if not file_name.lower().endswith((".xlsx", ".xlsm")):
                raise ValueError("当前只支持 .xlsx / .xlsm 格式。")

            job_id = uuid.uuid4().hex
            job_dir = ensure_writable_dir(base_dir / "jobs" / job_id)
            upload_dir = ensure_writable_dir(job_dir / "upload")
            input_path = upload_dir / file_name
            with input_path.open("wb") as target:
                shutil.copyfileobj(file_item.file, target)

            path_map_text = _form_value(form, "path_map")
            data_root = _form_value(form, "data_root")
            database_name = _form_value(form, "database_name")
            max_rows = _form_int(form, "max_rows")

            now = time.time()
            job = BatchJob(
                job_id=job_id,
                status="queued",
                stage="排队中",
                detail="任务已创建，等待分析线程启动。",
                input_name=file_name,
                created_at=now,
                updated_at=now,
            )
            with JOBS_LOCK:
                JOBS[job_id] = job

            thread = threading.Thread(
                target=run_job_thread,
                args=(job_id, input_path, job_dir / "output", path_map_text, data_root, database_name, max_rows),
                daemon=True,
            )
            thread.start()
            return job

        def _send_job(self, job_id: str) -> None:
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if not job:
                self._send_json({"ok": False, "error": "job_not_found"}, status=404)
                return
            self._send_json({"ok": True, "job": serialize_job(job)})

        def _send_download(self, job_id: str) -> None:
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if not job:
                self._send_json({"ok": False, "error": "job_not_found"}, status=404)
                return
            if job.status != "completed" or not job.output_zip:
                self._send_json({"ok": False, "error": "job_not_completed"}, status=409)
                return
            zip_path = Path(job.output_zip)
            if not zip_path.is_file():
                self._send_json({"ok": False, "error": "result_zip_missing"}, status=404)
                return
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(zip_path.name)[0] or "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="blu_agent_batch_result.zip"')
            self.send_header("Content-Length", str(zip_path.stat().st_size))
            self.end_headers()
            with zip_path.open("rb") as source:
                shutil.copyfileobj(source, self.wfile)

        def _send_json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, body_text: str, status: int = 200) -> None:
            body = body_text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return LocalBatchHandler


def run_job_thread(
    job_id: str,
    input_path: Path,
    output_dir: Path,
    path_map_text: str,
    data_root: str,
    database_name: str,
    max_rows: int | None,
) -> None:
    update_job(job_id, status="running", stage="启动中", detail="正在初始化 Excel 批量分析。")

    def progress(stage: str, detail: str) -> None:
        update_job(job_id, status="running", stage=stage, detail=detail)

    try:
        config = load_local_batch_config(
            path_map_text=path_map_text,
            data_root=data_root,
            database_name=database_name,
            max_rows=max_rows,
        )
        result = run_local_excel_batch_analysis(
            input_excel=input_path,
            output_dir=output_dir,
            config=config,
            progress_callback=progress,
        )
        update_job(
            job_id,
            status="completed",
            stage="完成",
            detail="分析完成，可以下载结果包。",
            output_zip=result.output_zip,
            output_excel=result.output_excel,
            counts={
                "total_rows": result.total_rows,
                "pending_rows": result.pending_rows,
                "processed_count": result.processed_count,
                "success_count": result.success_count,
                "failed_count": result.failed_count,
                "skipped_count": result.skipped_count,
            },
        )
    except Exception as exc:
        update_job(job_id, status="failed", stage="失败", detail=str(exc), error=str(exc))


def update_job(job_id: str, **changes: Any) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        for key, value in changes.items():
            if hasattr(job, key):
                setattr(job, key, value)
        job.updated_at = time.time()
        if "stage" in changes or "detail" in changes:
            job.history.append(
                {
                    "time": time.strftime("%H:%M:%S"),
                    "stage": str(job.stage),
                    "detail": str(job.detail),
                }
            )
            del job.history[:-80]


def serialize_job(job: BatchJob) -> dict[str, Any]:
    data = asdict(job)
    data["download_url"] = f"/api/jobs/{html.escape(job.job_id)}/download" if job.status == "completed" else ""
    return data


def _form_value(form: cgi.FieldStorage, name: str) -> str:
    if name not in form:
        return ""
    value = form.getfirst(name, "")
    return str(value or "").strip()


def _form_int(form: cgi.FieldStorage, name: str) -> int | None:
    value = _form_value(form, name)
    if not value:
        return None
    try:
        return max(1, int(value))
    except ValueError:
        raise ValueError(f"{name} 必须是正整数。")


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>布鲁 Agent 内网批量分析</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #111827;
      --muted: #526174;
      --line: #d8e0eb;
      --panel: #f6f8fb;
      --blue: #2563eb;
      --cyan: #12b3c7;
      --green: #13b981;
      --amber: #f59e0b;
      font-family: "Segoe UI", "Microsoft YaHei", Arial, sans-serif;
    }
    body { margin: 0; background: #fff; color: var(--ink); }
    main { max-width: 1180px; margin: 0 auto; padding: 44px 36px 56px; }
    header { display: flex; justify-content: space-between; gap: 24px; align-items: flex-start; margin-bottom: 34px; }
    h1 { margin: 0; font-size: 36px; line-height: 1.15; letter-spacing: 0; }
    .subtitle { color: var(--muted); font-size: 17px; margin-top: 10px; }
    .badge { background: #0b1220; color: #fff; border-radius: 8px; padding: 10px 16px; font-weight: 700; white-space: nowrap; }
    .grid { display: grid; grid-template-columns: 1.08fr 0.92fr; gap: 22px; }
    section { border: 1px solid var(--line); border-radius: 8px; padding: 22px; background: var(--panel); }
    h2 { margin: 0 0 16px; font-size: 22px; }
    label { display: block; font-weight: 700; margin: 16px 0 7px; }
    input, textarea { width: 100%; box-sizing: border-box; border: 1px solid #cbd5e1; border-radius: 6px; padding: 11px 12px; font: inherit; background: #fff; color: var(--ink); }
    textarea { min-height: 126px; resize: vertical; font-family: Consolas, "Microsoft YaHei", monospace; }
    .hint { color: var(--muted); font-size: 13px; line-height: 1.55; margin-top: 6px; }
    .actions { display: flex; gap: 12px; align-items: center; margin-top: 20px; }
    button, .download { border: 0; border-radius: 6px; background: var(--blue); color: #fff; font-weight: 800; padding: 12px 18px; cursor: pointer; text-decoration: none; display: inline-flex; align-items: center; }
    button:disabled { background: #9aa8bb; cursor: wait; }
    .download { background: var(--green); }
    .status { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin: 14px 0 18px; }
    .metric { background: #fff; border: 1px solid var(--line); border-left: 5px solid var(--cyan); border-radius: 8px; padding: 14px; }
    .metric strong { font-size: 24px; display: block; }
    .metric span { color: var(--muted); font-size: 13px; }
    .stage { background: #fff; border: 1px solid var(--line); border-left: 5px solid var(--amber); border-radius: 8px; padding: 16px; line-height: 1.55; }
    .log { height: 280px; overflow: auto; background: #0b1220; color: #dbeafe; border-radius: 8px; padding: 14px; font-family: Consolas, monospace; font-size: 13px; line-height: 1.55; }
    .log div { margin-bottom: 8px; }
    .error { color: #b91c1c; font-weight: 700; }
    @media (max-width: 900px) {
      main { padding: 28px 18px 42px; }
      header, .grid { display: block; }
      .badge { display: inline-block; margin-top: 18px; }
      section { margin-bottom: 18px; }
      .status { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>布鲁 Agent 内网批量分析</h1>
        <div class="subtitle">上传同格式 Excel，按待分析行逐条定位行车问题原因，并输出结果表与信号图示</div>
      </div>
      <div class="badge">本地 UI · 无飞书入口</div>
    </header>

    <div class="grid">
      <section>
        <h2>提交任务</h2>
        <form id="jobForm">
          <label for="excel">Excel 表格</label>
          <input id="excel" name="excel" type="file" accept=".xlsx,.xlsm" required>
          <div class="hint">需要包含 Agent_问题描述、Agent_h5数据路径、Agent_问题类型、Agent_问题状态、Agent_问题日期、Agent_问题时间、Agent_车号、Agent_车型 等列；H5 路径可留空，留空时按车号/车型/日期/时间自动查找；只处理状态为“待分析”的行。</div>

          <label for="path_map">路径映射</label>
          <textarea id="path_map" name="path_map" placeholder='{"E:\\ADAS_data":"/data/adas_data"}'></textarea>
          <div class="hint">Excel 里如果仍是 Windows 路径，需要映射到容器可访问的数据目录。也支持每行写 source=target。</div>

          <label for="data_root">数据根目录</label>
          <input id="data_root" name="data_root" placeholder="/data">
          <div class="hint">Excel 内相对路径会基于该目录查找；H5 路径留空时也会在该目录下按车号/车型/日期/时间自动定位。</div>

          <label for="database_name">数据库文件名</label>
          <input id="database_name" name="database_name" placeholder="留空则使用默认逻辑">

          <label for="max_rows">最多读取行数</label>
          <input id="max_rows" name="max_rows" type="number" min="1" placeholder="50">

          <div class="actions">
            <button id="submitBtn" type="submit">开始分析</button>
            <span id="formMsg" class="hint"></span>
          </div>
        </form>
      </section>

      <section>
        <h2>运行状态</h2>
        <div class="status">
          <div class="metric"><strong id="pending">0</strong><span>待分析</span></div>
          <div class="metric"><strong id="success">0</strong><span>成功</span></div>
          <div class="metric"><strong id="failed">0</strong><span>失败</span></div>
        </div>
        <div class="stage">
          <strong id="stage">未开始</strong>
          <div id="detail" class="hint">上传 Excel 后会显示当前处理进度。</div>
          <div id="downloadBox" style="margin-top:12px"></div>
        </div>
        <h2 style="margin-top:22px">日志</h2>
        <div id="log" class="log"></div>
      </section>
    </div>
  </main>
  <script>
    const form = document.getElementById("jobForm");
    const submitBtn = document.getElementById("submitBtn");
    const formMsg = document.getElementById("formMsg");
    let pollTimer = null;

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      submitBtn.disabled = true;
      formMsg.textContent = "正在上传并创建任务...";
      document.getElementById("downloadBox").innerHTML = "";
      try {
        const response = await fetch("/api/jobs", { method: "POST", body: new FormData(form) });
        const payload = await response.json();
        if (!payload.ok) throw new Error(payload.error || "创建任务失败");
        formMsg.textContent = "任务已创建。";
        renderJob(payload.job);
        if (pollTimer) clearInterval(pollTimer);
        pollTimer = setInterval(() => pollJob(payload.job.job_id), 1500);
      } catch (err) {
        formMsg.innerHTML = `<span class="error">${escapeHtml(err.message)}</span>`;
        submitBtn.disabled = false;
      }
    });

    async function pollJob(jobId) {
      const response = await fetch(`/api/jobs/${jobId}`);
      const payload = await response.json();
      if (!payload.ok) return;
      renderJob(payload.job);
      if (["completed", "failed"].includes(payload.job.status)) {
        clearInterval(pollTimer);
        submitBtn.disabled = false;
      }
    }

    function renderJob(job) {
      const counts = job.counts || {};
      document.getElementById("pending").textContent = counts.pending_rows ?? 0;
      document.getElementById("success").textContent = counts.success_count ?? 0;
      document.getElementById("failed").textContent = counts.failed_count ?? 0;
      document.getElementById("stage").textContent = job.stage || job.status;
      document.getElementById("detail").textContent = job.detail || "";
      const log = document.getElementById("log");
      log.innerHTML = (job.history || []).map(item => `<div>[${escapeHtml(item.time)}] ${escapeHtml(item.stage)}：${escapeHtml(item.detail)}</div>`).join("");
      log.scrollTop = log.scrollHeight;
      if (job.status === "completed" && job.download_url) {
        document.getElementById("downloadBox").innerHTML = `<a class="download" href="${job.download_url}">下载结果包</a>`;
      }
      if (job.status === "failed") {
        document.getElementById("downloadBox").innerHTML = `<span class="error">${escapeHtml(job.error || job.detail || "任务失败")}</span>`;
      }
    }

    function escapeHtml(value) {
      return String(value || "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
    }
  </script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Blu Agent local Excel batch analysis UI.")
    parser.add_argument("--host", default=os.getenv("LOCAL_BATCH_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("LOCAL_BATCH_PORT", "8080")))
    parser.add_argument("--runtime-dir", default=os.getenv("LOCAL_BATCH_UI_RUNTIME", ""))
    args = parser.parse_args()

    os.environ.setdefault("BATCH_ROOT_CAUSE_ANALYSIS_MODE", "deepseek_investigation")
    base_dir = ensure_writable_dir(args.runtime_dir or get_ui_runtime_dir())
    handler = create_handler(base_dir)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Blu local batch UI started: http://{args.host}:{args.port}", flush=True)
    print(f"Runtime: {base_dir}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
