from __future__ import annotations

import importlib.util
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote
from xml.etree import ElementTree as ET

import requests
from lark_oapi.api.im.v1 import GetFileRequest, GetMessageResourceRequest

from runtime_paths import (
    ensure_writable_dir,
    ensure_writable_download_file_path,
    ensure_writable_file_path,
    get_runtime_subdir,
    get_runtime_subprocess_env,
    safe_runtime_name,
)


ROOT = Path(__file__).resolve().parent

_RESOURCE_TOKEN_CACHE: dict[str, Any] = {"token": "", "expires_at": 0.0}
_RESOURCE_TOKEN_LOCK = threading.Lock()
_RESOURCE_PROBE_BYTES = 128 * 1024
_RESOURCE_CHUNK_BYTES = 8 * 1024 * 1024


def get_upload_dir() -> Path:
    return get_runtime_subdir("uploads")


def get_result_dir() -> Path:
    return get_runtime_subdir("analysis_results")


UPLOAD_DIR = get_upload_dir()
RESULT_DIR = get_result_dir()
CAN_CODE_ROOT = ROOT / "analysis_engine"
METRICS_DIR = CAN_CODE_ROOT / "metrics" / "code"
SIGNAL_LIB_DIR = CAN_CODE_ROOT
DATABASE_DIR = ROOT / "database"

EVENT_PATTERN = re.compile(r"\[(?:Event|Scene|Candidate)\s+(?P<body>[^\]]*)\]")
START_PATTERN = re.compile(r"(?:^|[,\s])start=(?P<value>[-+]?\d+(?:\.\d+)?)")
END_PATTERN = re.compile(r"(?:^|[,\s])end=(?P<value>[-+]?\d+(?:\.\d+)?)")
DURATION_PATTERN = re.compile(r"(?:^|[,\s])duration=(?P<value>[-+]?\d+(?:\.\d+)?)")
ROOT_CAUSE_CANDIDATE_METRICS = {
    "acc_unreasonable_low_speed_context_count",
    "acc_set_speed_reach",
}


def patch_python_can_blf_timestamp(can_module: Any) -> None:
    blf_module = getattr(getattr(can_module, "io", None), "blf", None)
    if blf_module is None or getattr(blf_module, "_feishuagent_timestamp_patch", False):
        return

    def safe_systemtime_to_timestamp(systemtime: Any) -> float:
        try:
            value = datetime.datetime(
                int(systemtime[0]),
                int(systemtime[1]),
                int(systemtime[3]),
                int(systemtime[4]),
                int(systemtime[5]),
                int(systemtime[6]),
                int(systemtime[7]) * 1000,
                tzinfo=datetime.timezone.utc,
            )
            return value.timestamp()
        except Exception:
            return 0.0

    blf_module.systemtime_to_timestamp = safe_systemtime_to_timestamp
    blf_module._feishuagent_timestamp_patch = True

METRIC_DESCRIPTIONS = {
    "abs_activation_count": ("ABS 激活", "检测制动防抱死 ABS 激活场景，通常对应低附着或急制动导致的稳定性介入。"),
    "tcs_activation_count": ("TCS 牵引力控制介入", "检测牵引力控制 TCS 激活场景，通常对应驱动轮打滑或附着不足。"),
    "vse_activation_count": ("VSE 车身稳定控制介入", "检测车身稳定控制 VSE 激活场景，通常对应横摆稳定性风险或车辆姿态异常。"),
    "acc_jerkiness": ("ACC 加速度抖动", "ACC 激活且未被驾驶员接管时，纵向加速度出现明显振荡或跳变。"),
    "acc_jerkiness_after_override_release": ("ACC 接管释放后加速度抖动", "驾驶员接管释放后，ACC 恢复控制阶段出现纵向加速度振荡或跳变。"),
    "acc_override_brake_request_jerkiness": ("ACC 接管期间制动请求抖动", "驾驶员接管或制动请求相关阶段出现加速度突变，可能影响舒适性。"),
    "acc_start_no_override_jerkiness": ("ACC 起步阶段抖动", "ACC 起步且未接管时出现纵向加速度波动，可能表现为起步顿挫。"),
    "acc_heavy_braking_count": ("ACC 急减速/重刹", "ACC 控制过程中出现较强减速度或急剧制动变化。"),
    "acc_heavy_braking_after_override_release_count": ("接管释放后 ACC 急减速", "驾驶员接管释放后，ACC 恢复阶段出现较强减速度。"),
    "acc_false_braking_context_count": ("ACC 误制动疑似场景", "ACC 有效且未接管时，纵向加速度出现突发负向波谷，并结合前车/Cut-in、曲率限速上下文判断是否缺少合理减速原因。"),
    "acc_missed_braking_count": ("ACC 漏制动疑似场景", "前方目标风险已经出现，但 ACC/AEB 制动或减速请求没有及时给出，且实车未形成对应减速。"),
    "acc_unreasonable_low_speed_context_count": ("ACC 不合理低速疑似场景", "ACC 有效且未接管时，车辆未达到 SetSpeed 或短时间明显掉速，并结合前车/Cut-in、曲率限速和 SetSpeed 变化判断是否缺少合理低速原因。"),
    "acc_following_distance_anomaly": ("ACC 跟车距离异常", "根据 ACCHdwyStg 时距设置和稳定跟车距离表计算期望车距，识别跟车过近、过远和距离波动，并结合前车目标、请求侧和响应侧信号解释原因。"),
    "acc_brake_rapid_toggle_decel_jump_count": ("ACC 制动快速切换导致减速度跳变", "ACC 制动类型或制动请求快速切换，并伴随减速度突变。"),
    "acc_rollback_count": ("ACC 低速溜车", "ACC 低速保持或起步阶段，车辆轮速方向与期望行驶方向不一致。"),
    "acc_lost_arbitration": ("ACC 控制权丢失", "ACC 请求状态出现 lost arbitration，表示控制权仲裁未保持。"),
    "acc_set_speed_reach": ("ACC 未达到设定车速", "ACC 激活且未接管时，车辆显示速度未达到设定速度要求。"),
    "lateral_steering_sudden": ("LCC 横向猛打", "LCC 激活期间方向盘角在 1 秒内突变超过阈值，并区分转向请求侧、车道几何诱因和执行器响应侧。"),
}

PROBLEM_GROUP_DEFINITIONS = {
    "stability_control": {
        "label": "车身稳定与附着控制",
        "description": "ABS、TCS、VSE 等稳定性和附着控制相关问题。",
        "metrics": {
            "abs_activation_count",
            "tcs_activation_count",
            "vse_activation_count",
        },
    },
    "acc_control": {
        "label": "ACC 跟车与纵向控制",
        "description": "ACC 舒适性、重刹、回退、控制权和设速相关问题。",
        "metrics": {
            "acc_jerkiness",
            "acc_jerkiness_after_override_release",
            "acc_override_brake_request_jerkiness",
            "acc_start_no_override_jerkiness",
            "acc_heavy_braking_count",
            "acc_heavy_braking_after_override_release_count",
            "acc_false_braking_context_count",
            "acc_missed_braking_count",
            "acc_unreasonable_low_speed_context_count",
            "acc_following_distance_anomaly",
            "acc_brake_rapid_toggle_decel_jump_count",
            "acc_rollback_count",
            "acc_lost_arbitration",
            "acc_set_speed_reach",
        },
    },
    "lateral_control": {
        "label": "LCC 横向控制",
        "description": "LCC 横向舒适性、转向请求和 EPS 响应相关问题。",
        "metrics": {
            "lateral_steering_sudden",
        },
    },
}
DEFAULT_PROBLEM_GROUPS = tuple(PROBLEM_GROUP_DEFINITIONS.keys())
_PERFORMANCE_METRIC_KNOWLEDGE: dict[str, dict[str, Any]] | None = None


@dataclass(frozen=True)
class PerformanceProblemPreset:
    key: str
    label: str
    aliases: tuple[str, ...]
    vehicle_signals: tuple[str, ...]
    algorithm_request_signals: tuple[str, ...]
    actuator_request_signals: tuple[str, ...]
    metrics: tuple[str, ...]
    algorithm_cause_signals: tuple[str, ...] = ()
    algorithm_cause_logic: str = ""
    root_cause_output_hint: str = ""
    note: str = ""


@dataclass(frozen=True)
class PerformanceAnalysisScope:
    problem_types: tuple[str, ...]
    problem_labels: tuple[str, ...]
    groups: tuple[str, ...]
    metrics: tuple[str, ...]
    notes: tuple[str, ...] = ()


PERFORMANCE_PRESET_TABLE_PATH = ROOT / "config" / "performance_problem_presets.xlsx"
PERFORMANCE_PRESET_REQUIRED_COLUMNS = (
    "问题类型",
    "触发词",
    "实车表现关注信号",
    "算法请求关注信号",
    "执行器请求关注信号",
    "关联问题筛选代码",
    "说明",
)
PERFORMANCE_PRESET_OPTIONAL_COLUMNS = (
    "算法请求诱因关注信号",
    "算法请求诱因判断逻辑",
    "行车问题原因分析输出要求",
)
PERFORMANCE_PRESET_COLUMNS = PERFORMANCE_PRESET_REQUIRED_COLUMNS + PERFORMANCE_PRESET_OPTIONAL_COLUMNS


def _canonical_signal_short_name(name: str) -> str:
    short_name = str(name or "").rsplit(".", 1)[-1].strip()
    if len(short_name) > 1 and short_name.startswith("I") and short_name[1].isupper():
        return short_name[1:]
    return short_name


ROOT_CAUSE_EXTRA_SIGNALS = (
    # Vehicle response / phenomena layer.
    "IMU_Yaw_Long_Acc_037_M2.IIMULonAccSec",
    "IMU_Yaw_Long_Acc_037_M2.IIMULonAcc_adas",
    "PPEI_Vehicle_Speed_and_Distance_209_M1.IVehSpdAvgDrvn_1",
    "PPEI_Vehicle_Speed_and_Distance_208_M2.IVehSpdAvgNDrvn",
    "Adaptive_Cruise_Disp_Stat.IACCAct376",
    "Adaptive_Cruise_Disp_Stat.IACCHdwyStg",
    "PPEI_Propulsion_General_Data_1_0C2_M.IAccPdlOvrrdAtv",
    # ACC propulsion request and response.
    "PPEI_Adaptive_Cruise_Axl_Trq_Req_194_M.IACCATC_ACCAct",
    "PPEI_Adaptive_Cruise_Axl_Trq_Req_194_M.IACCATC_ACCTyp",
    "PPEI_Adaptive_Cruise_Axl_Trq_Req_194_M.IACCATC_AxlTrqRq",
    "PPEI_Adaptive_Cruise_Axl_Trq_Req_194_M.IACCATC_DrvAstdGoSt",
    "PPEI_Propulsion_General_Data_1_0C2_M.IACCATCS_RqStat",
    "PPEI_Propulsion_General_Data_1_0C2_M.IACCATCS_LmtgStat",
    "PPEI_Engine_Torque_Status_4_08D_M.IActAxleTrq",
    "PPEI_Engine_Torque_Status_4_08D_M.IActAxleTrqV",
    # ACC/AEB brake request, C-CAN only for ACC brake request attribution.
    "ADAS_Command_Ext1.IACCBSCE_ACCAct1",
    "ADAS_Command_Ext1.IACCBSCE_AutBrkTp1",
    "ADAS_Command_Ext1.IACCBSCE_ACCAccl1",
    "ADAS_Command_Ext1.IACCBSCE_ACCTrq1",
    "ADAS_Command_Ext1.IFCACBSC_AutBrkRqTp1",
    "ADAS_Command_Ext1.IFCACBSC_CtrlTrq1",
    # Brake response and driver override.
    "PPEI_Chassis_General_Data_2_191_M.IAutoBrkngAvlbl",
    "PPEI_Chassis_General_Data_2_191_M.IAutoBrkngAct",
    "PPEI_Chassis_General_Data_3_0AC_M.IACCBrkngAct",
    "Automatic_Braking_Status_044_M.IBrkSysAutBrkStat",
    "PPEI_Chassis_General_Data_2_191_M.IBrkSysAutBrkFld",
    "Automatic_Braking_Status_044_M.IFSRACCBrkngReqDenied",
    "PPEI_Chassis_General_Data_1_031_M.IBrkPedApp",
    "PPEI_Brake_Apply_Status_030_M.IBrkPedTrvlAchvd",
    "ICSTBATS_TrqVl",
    "IRegenBrkAxTrqAch",
    # Algorithm-request cause context. Some names are temporary aliases until the real bus names are confirmed.
    "LeadTargetId",
    "LeadTargetDistance",
    "LeadTargetVelocity",
    "LeadTargetAccel",
    "LeadTargetTTC",
    "CutInTargetId",
    "CutInTargetDistance",
    "CutInTargetVelocity",
    "CutInTargetAccel",
    "CutInTargetTTC",
    "CurvatureLimitSpeed",
    "Curvature",
    "CurvatureLimitEnable",
    "Adaptive_Cruise_Disp_Stat.IACCDrvrSeltdSpd",
    "Adaptive_Cruise_Disp_Stat.IACCHdwyStg",
    "FLT1_Global_ID",
    "FLT1_Long_Range",
    "FLT1_Long_Velocity",
    "FLT1_Long_Accel",
    "FLT3_Global_ID",
    "FLT3_Long_Range",
    "FLT3_Long_Velocity",
    "FLT3_Long_Accel",
    "FLT5_Global_ID",
    "FLT5_Long_Range",
    "FLT5_Long_Velocity",
    "FLT5_Long_Accel",
    "IFCMSpdLmtSnSpdLmtVau",
    "IFCMSpdLmtSnSpdLmtVauAs",
    "LaneModelC2",
    "Left_Lane_A2",
    "Right_Lane_A2",
    "MPC_DebugMsg_Reserved_18.Debug_single_Reserved_51",
    "Eth_Debug_CAN5/MPC_DebugMsg_Reserved_18.Debug_single_Reserved_51",
    "Debug_single_Reserved_51",
)
ROOT_CAUSE_EXTRA_SIGNAL_SHORT_NAMES = tuple(
    sorted({_canonical_signal_short_name(name) for name in ROOT_CAUSE_EXTRA_SIGNALS})
)


@dataclass
class MetricIssue:
    metric_name: str
    details: list[str]
    statistics: dict[str, Any]
    events: list[dict[str, float]]
    image_paths: list[Path] = field(default_factory=list)


@dataclass
class AnalysisResult:
    status: str
    summary: str
    problems: list[MetricIssue] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    local_file: Path | None = None
    database_path: Path | None = None
    selected_problem_groups: list[str] = field(default_factory=list)
    selected_problem_types: list[str] = field(default_factory=list)
    selected_metric_names: list[str] = field(default_factory=list)
    polished_summary: str = ""


def safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "uploaded_file")
    return cleaned.strip(" .") or "uploaded_file"


def _response_error(response: Any) -> str:
    if response is None:
        return "接口没有返回响应"
    log_id = ""
    try:
        log_id = response.get_log_id()
    except Exception:
        log_id = ""
    suffix = f", log_id={log_id}" if log_id else ""
    return f"code={getattr(response, 'code', None)}, msg={getattr(response, 'msg', None)}{suffix}"


def _write_response_file(response: Any, output_path: Path) -> None:
    source = getattr(response, "file", None)
    if source is None:
        raise RuntimeError("飞书文件下载接口没有返回文件流")

    with output_path.open("wb") as target:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            target.write(chunk)


def _trim_command_output(text: str, limit: int = 2000) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "...(已截断)"


def _feishu_openapi_error(response: requests.Response) -> str:
    code: Any = response.status_code
    message = response.reason or "请求失败"
    try:
        body = response.json()
        if isinstance(body, dict):
            code = body.get("code", code)
            message = body.get("msg") or body.get("message") or message
    except (ValueError, TypeError):
        pass
    log_id = response.headers.get("X-Tt-Logid") or response.headers.get("X-Lark-Request-Id") or ""
    suffix = f", log_id={log_id}" if log_id else ""
    return f"code={code}, msg={str(message)[:300]}{suffix}"


def _resource_tenant_access_token(force_refresh: bool = False) -> str:
    """Use the same app credentials as the running bot, not lark-cli's profile."""
    now = time.time()
    if (
        not force_refresh
        and _RESOURCE_TOKEN_CACHE.get("token")
        and now < float(_RESOURCE_TOKEN_CACHE.get("expires_at") or 0.0)
    ):
        return str(_RESOURCE_TOKEN_CACHE["token"])

    with _RESOURCE_TOKEN_LOCK:
        now = time.time()
        if (
            not force_refresh
            and _RESOURCE_TOKEN_CACHE.get("token")
            and now < float(_RESOURCE_TOKEN_CACHE.get("expires_at") or 0.0)
        ):
            return str(_RESOURCE_TOKEN_CACHE["token"])

        from feishu_config import get_feishu_credentials

        app_id, app_secret = get_feishu_credentials()
        response = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=20,
        )
        if not response.ok:
            raise RuntimeError(f"获取当前机器人访问令牌失败：{_feishu_openapi_error(response)}")
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError("获取当前机器人访问令牌失败：飞书返回了非 JSON 响应") from exc
        if body.get("code") != 0:
            raise RuntimeError(
                "获取当前机器人访问令牌失败："
                f"code={body.get('code')}, msg={str(body.get('msg') or '')[:300]}"
            )
        token = str(body.get("tenant_access_token") or "")
        if not token:
            raise RuntimeError("获取当前机器人访问令牌失败：响应中缺少 tenant_access_token")
        try:
            expires_in = int(body.get("expire") or 7200)
        except (TypeError, ValueError):
            expires_in = 7200
        _RESOURCE_TOKEN_CACHE["token"] = token
        _RESOURCE_TOKEN_CACHE["expires_at"] = time.time() + max(60, expires_in - 300)
        return token


def _parse_content_range(value: str) -> tuple[int, int, int | None]:
    match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", (value or "").strip(), re.IGNORECASE)
    if not match:
        raise RuntimeError(f"飞书分片响应缺少有效 Content-Range：{value or '空'}")
    start = int(match.group(1))
    end = int(match.group(2))
    total = None if match.group(3) == "*" else int(match.group(3))
    return start, end, total


def _download_message_resource_with_current_app(
    message_id: str,
    file_key: str,
    resource_type: str,
    output_path: Path,
    progress_callback: Callable[[str, str], None] | None = None,
    default_name: str | None = None,
) -> Path:
    """Range-download a large message resource with the running bot's app identity."""
    output_path = ensure_writable_download_file_path(
        output_path,
        default_name or output_path.name or "download.bin",
    )
    ensure_writable_dir(output_path.parent)
    _cleanup_download_temp_files(output_path)
    temp_path = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    api_type = "image" if resource_type == "image" else "file"
    url = (
        "https://open.feishu.cn/open-apis/im/v1/messages/"
        f"{quote(message_id, safe='')}/resources/{quote(file_key, safe='')}"
    )
    total_size: int | None = None
    offset = 0

    try:
        with temp_path.open("wb") as target:
            while total_size is None or offset < total_size:
                chunk_size = _RESOURCE_PROBE_BYTES if offset == 0 else _RESOURCE_CHUNK_BYTES
                range_end = offset + chunk_size - 1
                last_error = ""
                downloaded = False
                refresh_token = False

                for attempt in range(3):
                    token = _resource_tenant_access_token(force_refresh=refresh_token)
                    refresh_token = False
                    try:
                        response = requests.get(
                            url,
                            params={"type": api_type},
                            headers={
                                "Authorization": f"Bearer {token}",
                                "Range": f"bytes={offset}-{range_end}",
                            },
                            stream=True,
                            timeout=(20, max(60, _download_stall_seconds())),
                        )
                    except requests.RequestException as exc:
                        last_error = f"网络异常：{exc}"
                        if attempt < 2:
                            time.sleep(1.5 * (attempt + 1))
                        continue

                    with response:
                        if response.status_code not in {200, 206}:
                            last_error = _feishu_openapi_error(response)
                            if attempt < 2 and response.status_code == 401:
                                refresh_token = True
                                continue
                            if attempt < 2 and response.status_code >= 500:
                                time.sleep(1.5 * (attempt + 1))
                                continue
                            break

                        if response.status_code == 206:
                            response_start, response_end, response_total = _parse_content_range(
                                response.headers.get("Content-Range", "")
                            )
                            if response_start != offset or response_end < response_start:
                                raise RuntimeError(
                                    "飞书返回的文件分片范围不连续："
                                    f"期望从 {offset} 开始，实际为 {response_start}-{response_end}"
                                )
                            if response_total is not None:
                                if total_size is not None and response_total != total_size:
                                    raise RuntimeError("下载过程中飞书返回的文件总大小发生变化")
                                total_size = response_total
                        elif offset != 0:
                            raise RuntimeError("飞书在续传阶段忽略了 Range 请求，无法保证文件完整性")

                        expected_next_offset = response_end + 1 if response.status_code == 206 else None
                        before = offset
                        for data in response.iter_content(chunk_size=1024 * 1024):
                            if data:
                                target.write(data)
                                offset += len(data)
                        target.flush()
                        if offset <= before:
                            raise RuntimeError("飞书返回了空文件分片")
                        if expected_next_offset is not None and offset != expected_next_offset:
                            raise RuntimeError(
                                "飞书返回的文件分片长度不完整："
                                f"响应声明结束于 {expected_next_offset - 1}，实际写入到 {offset - 1}"
                            )
                        if response.status_code == 200:
                            total_size = offset
                        downloaded = True
                        break

                if not downloaded:
                    raise RuntimeError(f"分片请求失败：{last_error or '未知错误'}")

                total_text = f" / {_format_bytes(total_size)}" if total_size is not None else ""
                _notify_download_progress(
                    progress_callback,
                    "下载文件中",
                    f"正在下载 `{output_path.name}`，已写入 {_format_bytes(offset)}{total_text}。",
                )

        if offset <= 0 or (total_size is not None and offset != total_size):
            raise RuntimeError(
                f"文件下载不完整：已写入 {_format_bytes(offset)}，"
                f"预期 {_format_bytes(total_size or 0)}"
            )
        temp_path.replace(output_path)
        _notify_download_progress(
            progress_callback,
            "下载完成",
            f"文件下载完成，大小 {_format_bytes(output_path.stat().st_size)}，正在准备分析。",
        )
        return output_path
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _lark_cli_candidates() -> list[str]:
    configured = os.getenv("FEISHU_RESOURCE_DOWNLOAD_CLI", "").strip()
    candidates = [configured] if configured else []
    candidates.extend(["lark-cli.cmd", "lark-cli"])
    result = []
    for candidate in candidates:
        if candidate and candidate not in result:
            result.append(candidate)
    return result


def _download_timeout_seconds() -> int:
    try:
        return max(60, int(os.getenv("FEISHU_RESOURCE_DOWNLOAD_TIMEOUT_SECONDS", "3600")))
    except (TypeError, ValueError):
        return 3600


def _download_stall_seconds() -> int:
    try:
        return max(30, int(os.getenv("FEISHU_RESOURCE_DOWNLOAD_STALL_SECONDS", "120")))
    except (TypeError, ValueError):
        return 120


def _download_progress_interval_seconds() -> int:
    try:
        return max(5, int(os.getenv("FEISHU_RESOURCE_DOWNLOAD_PROGRESS_INTERVAL_SECONDS", "15")))
    except (TypeError, ValueError):
        return 15


def _cleanup_download_temp_files(output_path: Path) -> None:
    pattern = f".{output_path.name}.*.tmp"
    for temp_file in output_path.parent.glob(pattern):
        try:
            temp_file.unlink(missing_ok=True)
        except Exception as exc:
            print(f"清理下载临时文件失败: {temp_file} {exc}", flush=True)


def _download_visible_size(output_path: Path) -> int:
    sizes: list[int] = []
    try:
        if output_path.exists():
            sizes.append(output_path.stat().st_size)
    except Exception:
        pass
    pattern = f".{output_path.name}.*.tmp"
    for temp_file in output_path.parent.glob(pattern):
        try:
            sizes.append(temp_file.stat().st_size)
        except Exception:
            pass
    return max(sizes, default=0)


def _download_identity_candidates() -> list[str]:
    raw = os.getenv("FEISHU_RESOURCE_DOWNLOAD_IDENTITIES") or "user,bot"
    identities: list[str] = []
    for item in re.split(r"[,;\s]+", raw.strip()):
        identity = item.strip().lower()
        if identity in {"user", "bot"} and identity not in identities:
            identities.append(identity)
    return identities or ["user", "bot"]


def _format_bytes(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{int(size)} B"


def _notify_download_progress(
    progress_callback: Callable[[str, str], None] | None,
    stage: str,
    detail: str,
) -> None:
    if progress_callback is None:
        return
    try:
        progress_callback(stage, detail)
    except Exception as exc:
        print(f"download progress callback failed: {exc}", flush=True)


def _terminate_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        process.kill()


def _read_log_tail(path: Path, limit: int = 2000) -> str:
    try:
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        return _trim_command_output(text, limit)
    except Exception as exc:
        return f"读取日志失败：{exc}"


def _download_with_lark_cli_once(
    message_id: str,
    file_key: str,
    resource_type: str,
    output_path: Path,
    identity: str,
    progress_callback: Callable[[str, str], None] | None = None,
    default_name: str | None = None,
) -> Path:
    output_path = ensure_writable_download_file_path(
        output_path,
        default_name or output_path.name or "download.bin",
    )
    output_dir = ensure_writable_dir(output_path.parent)
    output_name = output_path.name
    cli_type = "image" if resource_type == "image" else "file"
    timeout = _download_timeout_seconds()
    stall_seconds = _download_stall_seconds()
    progress_interval = _download_progress_interval_seconds()
    _cleanup_download_temp_files(output_path)
    try:
        output_path.unlink(missing_ok=True)
    except Exception:
        pass

    last_error = ""
    for executable in _lark_cli_candidates():
        resolved = shutil.which(executable)
        if not resolved:
            last_error = f"未找到 {executable}"
            continue

        command = [
            resolved,
            "im",
            "+messages-resources-download",
            "--message-id",
            message_id,
            "--file-key",
            file_key,
            "--type",
            cli_type,
            "--output",
            output_name,
            "--as",
            identity,
            "--format",
            "json",
        ]
        log_stem = safe_runtime_name(output_path.name or "download", "download", max_length=120)
        stdout_log = ensure_writable_file_path(
            output_path.with_name(f"{log_stem}.lark-cli.out.log"),
            f"{log_stem}.lark-cli.out.log",
        )
        stderr_log = ensure_writable_file_path(
            output_path.with_name(f"{log_stem}.lark-cli.err.log"),
            f"{log_stem}.lark-cli.err.log",
        )
        try:
            _notify_download_progress(
                progress_callback,
                "下载文件中",
                f"正在使用 {identity} 身份分片下载文件，当前已写入 {_format_bytes(_download_visible_size(output_path))}。",
            )
            finished_ok = False
            with stdout_log.open("w", encoding="utf-8", errors="replace") as stdout_file, stderr_log.open(
                "w",
                encoding="utf-8",
                errors="replace",
            ) as stderr_file:
                process = subprocess.Popen(
                    command,
                    cwd=str(output_dir),
                    env=get_runtime_subprocess_env(),
                    stdout=stdout_file,
                    stderr=stderr_file,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                start_time = time.monotonic()
                last_progress_time = start_time
                last_notify_time = 0.0
                last_size = _download_visible_size(output_path)
                timeout_reason = ""

                while True:
                    return_code = process.poll()
                    current_size = _download_visible_size(output_path)
                    now = time.monotonic()

                    if current_size > last_size:
                        last_size = current_size
                        last_progress_time = now

                    if now - last_notify_time >= progress_interval:
                        _notify_download_progress(
                            progress_callback,
                            "下载文件中",
                            f"正在下载 `{output_path.name}`（{identity} 身份），已写入 {_format_bytes(current_size)}。",
                        )
                        last_notify_time = now

                    if return_code is not None:
                        finished_ok = return_code == 0
                        break
                    if now - start_time > timeout:
                        timeout_reason = f"总下载时间超过 {timeout} 秒"
                        _terminate_process_tree(process)
                        break
                    if now - last_progress_time > stall_seconds:
                        timeout_reason = f"{stall_seconds} 秒内下载文件没有增长，可能卡在飞书资源下载阶段"
                        _terminate_process_tree(process)
                        break
                    time.sleep(2)

                if timeout_reason:
                    last_error = timeout_reason
                elif finished_ok and output_path.exists() and output_path.stat().st_size > 0:
                    try:
                        stdout_log.unlink(missing_ok=True)
                        stderr_log.unlink(missing_ok=True)
                    except Exception:
                        pass
                    _notify_download_progress(
                        progress_callback,
                        "下载完成",
                        f"文件下载完成，大小 {_format_bytes(output_path.stat().st_size)}，正在准备分析。",
                    )
                    return output_path
                elif finished_ok:
                    last_error = f"{executable} 退出码=0，但未生成有效文件"
                else:
                    last_error = f"{executable} 退出码={process.returncode}"
        except Exception as exc:
            last_error = f"{executable} 执行异常：{exc}"
            continue

        last_error = (
            f"{last_error}; "
            f"stdout={_read_log_tail(stdout_log)}; "
            f"stderr={_read_log_tail(stderr_log)}"
        )

    raise RuntimeError(f"{identity} 身份下载失败：{last_error or 'lark-cli 分片下载失败'}")


def _download_with_lark_cli(
    message_id: str,
    file_key: str,
    resource_type: str,
    output_path: Path,
    progress_callback: Callable[[str, str], None] | None = None,
    default_name: str | None = None,
) -> Path:
    errors: list[str] = []
    for identity in _download_identity_candidates():
        _notify_download_progress(
            progress_callback,
            "下载文件中",
            f"正在尝试使用 {identity} 身份进行飞书分片下载。",
        )
        try:
            return _download_with_lark_cli_once(
                message_id,
                file_key,
                resource_type,
                output_path,
                identity,
                progress_callback,
                default_name=default_name,
            )
        except Exception as exc:
            errors.append(str(exc))
            _cleanup_download_temp_files(output_path)
    raise RuntimeError("；".join(errors) or "lark-cli 分片下载失败")


def download_uploaded_file(
    rest_client,
    file_info: dict[str, Any],
    session_token: str,
    progress_callback: Callable[[str, str], None] | None = None,
) -> Path:
    file_key = str(file_info.get("file_key") or "").strip()
    message_id = str(file_info.get("message_id") or "").strip()
    file_name = safe_runtime_name(safe_filename(str(file_info.get("file_name") or "uploaded_file")), "uploaded_file")
    if not file_key:
        raise ValueError("上传文件缺少 file_key，无法下载文件内容")

    output_dir = ensure_writable_dir(get_upload_dir() / safe_runtime_name(session_token, "session"))
    output_path = ensure_writable_download_file_path(output_dir / file_name, file_name or "uploaded_file")

    errors: list[str] = []
    message_type = str(file_info.get("message_type") or "file").strip().lower()
    resource_type = "image" if message_type == "image" else "file"

    if message_id:
        _notify_download_progress(
            progress_callback,
            "下载文件中",
            "正在向飞书请求消息附件资源。",
        )
        for attempt in range(3):
            try:
                request = (
                    GetMessageResourceRequest.builder()
                    .message_id(message_id)
                    .file_key(file_key)
                    .type(resource_type)
                    .build()
                )
                response = rest_client.im.v1.message_resource.get(request)
                if response.success():
                    _write_response_file(response, output_path)
                    _notify_download_progress(
                        progress_callback,
                        "下载完成",
                        f"文件下载完成，大小 {_format_bytes(output_path.stat().st_size)}，正在准备分析。",
                    )
                    return output_path
                code = str(getattr(response, "code", "") or "")
                if code == "234037":
                    errors.append("消息资源接口提示文件超过单次下载限制，切换到飞书分片下载。")
                    break
                errors.append(
                    f"消息资源下载第 {attempt + 1} 次失败：{_response_error(response)}"
                )
            except Exception as exc:
                errors.append(f"消息资源下载第 {attempt + 1} 次异常：{exc}")
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    else:
        errors.append("缺少原始文件消息 message_id，无法使用飞书消息资源下载接口")

    if message_id:
        try:
            _notify_download_progress(
                progress_callback,
                "下载文件中",
                "文件较大，正在使用当前机器人身份进行分片下载。",
            )
            _download_message_resource_with_current_app(
                message_id,
                file_key,
                resource_type,
                output_path,
                progress_callback,
                default_name=file_name or "uploaded_file",
            )
            return output_path
        except Exception as exc:
            errors.append(f"当前机器人身份分片下载失败：{exc}")

        if os.getenv("FEISHU_RESOURCE_DOWNLOAD_USE_LARK_CLI_FALLBACK", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            try:
                _download_with_lark_cli(
                    message_id,
                    file_key,
                    resource_type,
                    output_path,
                    progress_callback,
                    default_name=file_name or "uploaded_file",
                )
                return output_path
            except Exception as exc:
                errors.append(f"兼容模式分片下载失败：{exc}")

    try:
        request = GetFileRequest.builder().file_key(file_key).build()
        fallback_response = rest_client.im.v1.file.get(request)
        if fallback_response.success():
            _write_response_file(fallback_response, output_path)
            _notify_download_progress(
                progress_callback,
                "下载完成",
                f"文件下载完成，大小 {_format_bytes(output_path.stat().st_size)}，正在准备分析。",
            )
            return output_path
        fallback_error = _response_error(fallback_response)
        if str(getattr(fallback_response, "code", "")) == "234008":
            fallback_error += "（该接口通常只能下载应用自己发送的资源，不能作为用户群附件的主下载方式）"
        errors.append(f"应用文件下载兜底失败：{fallback_error}")
    except Exception as exc:
        errors.append(f"应用文件下载兜底异常：{exc}")

    raise RuntimeError(
        "下载飞书文件失败：\n"
        + "\n".join(f"- {item}" for item in errors)
        + "\n建议：请直接在当前群重新上传文件，等待上传完成后再触发分析；"
        "如果仍失败，请检查机器人是否有消息资源下载权限。"
    )

    return output_path


def run_performance_analysis(
    local_file: Path,
    database_name: str,
    selected_problem_groups: list[str] | None = None,
    request_text: str | None = None,
) -> AnalysisResult:
    scope = resolve_performance_analysis_scope(request_text or "", selected_problem_groups)
    normalized_groups = list(scope.groups)
    selected_metrics = list(scope.metrics)
    database_path = DATABASE_DIR / database_name
    result = AnalysisResult(
        status="ok",
        summary="",
        local_file=local_file,
        database_path=database_path,
        selected_problem_groups=normalized_groups,
        selected_problem_types=list(scope.problem_labels),
        selected_metric_names=selected_metrics,
    )
    result.warnings.extend(scope.notes)

    if not database_path.exists():
        result.status = "error"
        result.summary = f"未找到选择的数据库文件：{database_name}"
        return result
    if not selected_metrics:
        result.status = "error"
        result.summary = "性能问题预设表没有匹配到可执行指标，请检查关联问题筛选代码配置。"
        return result

    suffix = local_file.suffix.lower()
    if suffix == ".blf":
        try:
            h5_file, convert_stats = convert_blf_to_h5(local_file, database_path, normalized_groups, selected_metrics)
            result.warnings.append(
                "BLF 已先转换为 H5："
                f"{convert_stats['decoded_frames']} / {convert_stats['total_frames']} 帧被目标指标信号解码。"
            )
            local_file = h5_file
            result.local_file = h5_file
        except ModuleNotFoundError as exc:
            result.status = "missing_dependency"
            result.summary = f"BLF 转 H5 缺少 Python 依赖：{exc.name}"
            result.warnings.append("请安装 python-can、cantools、h5py、numpy。")
            return result
        except Exception as exc:
            result.status = "needs_converter"
            result.summary = (
                "已收到并下载 BLF 文件，但自动转换为 H5 失败，无法继续运行 metrics/code。"
                f"失败原因：{exc}"
            )
            result.warnings.append(f"本地文件：{local_file}")
            result.warnings.append(f"数据库文件：{database_path}")
            return result

    suffix = local_file.suffix.lower()
    if suffix not in {".h5", ".hdf5"}:
        result.status = "unsupported_file"
        result.summary = f"当前性能分析支持 H5；BLF 需先转换。收到的文件类型是 `{suffix or '无后缀'}`。"
        return result

    try:
        metric_result = run_h5_metrics(local_file, normalized_groups, selected_metrics)
    except ModuleNotFoundError as exc:
        result.status = "missing_dependency"
        result.summary = f"运行性能分析缺少 Python 依赖：{exc.name}"
        result.warnings.append("请安装 h5py；如需图示还需要 pillow。")
        return result
    except Exception as exc:
        result.status = "error"
        result.summary = f"性能分析执行失败：{exc}"
        return result

    result.problems = metric_result
    metric_count = len(selected_metrics)
    if result.problems:
        total_events = sum(len(problem.events) for problem in result.problems)
        result.summary = (
            f"共执行 {metric_count} 项性能指标，识别到 "
            f"{len(result.problems)} 类问题，事件/场景 {total_events} 个。"
        )
    else:
        result.summary = f"共执行 {metric_count} 项性能指标，未识别到已定义的性能问题。"
    return result


def run_h5_metrics(
    h5_file: Path,
    selected_groups: list[str] | None = None,
    selected_metrics: list[str] | None = None,
) -> list[MetricIssue]:
    add_can_code_to_path()
    from signal_lib import FolderSession

    modules = load_metric_modules(selected_groups, selected_metrics)
    preprocess_signals = sorted(
        {
            signal
            for module in modules.values()
            for signal in (
                list(getattr(module, "REQUIRED_SIGNALS", []) or [])
                + list(getattr(module, "OPTIONAL_SIGNALS", []) or [])
            )
        }
    )
    session = FolderSession(h5_file, preprocess_signals=preprocess_signals)
    folder_context = {
        "folder_path": str(h5_file.parent),
        "h5_files": [str(h5_file)],
        "primary_h5_file": str(h5_file),
        "session": session,
    }

    issues: list[MetricIssue] = []
    image_root = ensure_writable_dir(
        get_result_dir() / f"{safe_runtime_name(h5_file.stem, 'h5')}_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    )
    for metric_name, module in modules.items():
        required = list(getattr(module, "REQUIRED_SIGNALS", []) or [])
        if getattr(module, "SIGNAL_CANDIDATES", None):
            missing = []
        else:
            missing = session.missing_signals(required)
        if missing:
            continue

        runner = getattr(module, "__run_haoran_data_run__", None)
        if runner is None:
            continue

        try:
            raw_result = runner(folder_context)
        except Exception as exc:
            issues.append(
                MetricIssue(
                    metric_name=metric_name,
                    details=[f"指标执行失败：{exc}"],
                    statistics={"Bad Case": 1, "All Case Count": 0, "Severity Score": 0.0},
                    events=[],
                )
            )
            continue

        if not isinstance(raw_result, tuple) or len(raw_result) not in {2, 3}:
            continue

        bad_case = bool(raw_result[0])
        details = [str(item) for item in (raw_result[1] or [])]
        statistics = dict(raw_result[2] if len(raw_result) == 3 and isinstance(raw_result[2], dict) else {})
        if not _should_keep_metric_issue(metric_name, bad_case, statistics):
            continue

        events = parse_events("\n".join(details))
        issue = MetricIssue(
            metric_name=metric_name,
            details=details,
            statistics=statistics,
            events=events,
        )
        issue.image_paths = build_issue_plots(session, metric_name, module, issue, image_root)
        issues.append(issue)

    return issues


def _should_keep_metric_issue(metric_name: str, bad_case: bool, statistics: dict[str, Any]) -> bool:
    bad_count = safe_int(statistics.get("Bad Case", 0))
    if bad_case or bad_count > 0:
        return True

    if metric_name not in ROOT_CAUSE_CANDIDATE_METRICS:
        return False

    candidate_count = max(
        safe_int(statistics.get("Candidate Event Count", 0)),
        safe_int(statistics.get("Under Target Candidate Count", 0)),
        safe_int(statistics.get("All Case Count", 0)),
    )
    return candidate_count > 0


def convert_blf_to_h5(
    blf_file: Path,
    database_path: Path,
    selected_groups: list[str] | None = None,
    selected_metrics: list[str] | None = None,
) -> tuple[Path, dict[str, int]]:
    import can
    import cantools
    import h5py
    import numpy as np

    patch_python_can_blf_timestamp(can)

    modules = load_metric_modules(selected_groups, selected_metrics)
    target_messages = {
        signal.split(".", 1)[0]
        for module in modules.values()
        for signal in (
            list(getattr(module, "REQUIRED_SIGNALS", []) or [])
            + list(getattr(module, "OPTIONAL_SIGNALS", []) or [])
        )
        if "." in signal
    }

    database = cantools.database.load_file(str(database_path), strict=False)
    root_cause_short_names = set(root_cause_extra_signal_short_names())
    for message in getattr(database, "messages", []) or []:
        for signal in getattr(message, "signals", []) or []:
            signal_name = _canonical_signal_short_name(getattr(signal, "name", ""))
            if signal_name in root_cause_short_names:
                target_messages.add(message.name)
                break

    output_dir = ensure_writable_dir(
        get_result_dir() / f"{safe_runtime_name(blf_file.stem, 'blf')}_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    )
    h5_path = output_dir / "_msd_can_data.h5"

    rows_by_message: dict[str, list[dict[str, Any]]] = {}
    total_frames = 0
    decoded_frames = 0
    first_timestamp: float | None = None

    with can.BLFReader(str(blf_file)) as reader:
        for frame in reader:
            total_frames += 1
            if first_timestamp is None:
                first_timestamp = float(frame.timestamp)
            try:
                message = database.get_message_by_frame_id(int(frame.arbitration_id))
            except Exception:
                continue

            if target_messages and message.name not in target_messages:
                continue

            try:
                decoded = message.decode(
                    bytes(frame.data),
                    decode_choices=False,
                    scaling=True,
                    allow_truncated=True,
                )
            except TypeError:
                decoded = message.decode(bytes(frame.data), decode_choices=False, scaling=True)
            except Exception:
                continue

            timestamp = float(frame.timestamp) - float(first_timestamp or frame.timestamp)
            row = {"timestamp": timestamp}
            for signal_name, value in decoded.items():
                row[str(signal_name)] = to_float_or_nan(value)

            rows_by_message.setdefault(message.name, []).append(row)
            decoded_frames += 1

    if not rows_by_message:
        raise RuntimeError(
            "BLF 中没有解码出 metrics 所需消息；请确认上传文件和所选 ARXML 数据库版本匹配。"
        )

    with h5py.File(h5_path, "w") as h5_file:
        channel = h5_file.create_group("CAN/Channel1")
        for message_name, rows in sorted(rows_by_message.items()):
            signal_names = sorted({key for row in rows for key in row.keys() if key != "timestamp"})
            dtype = [("timestamp", "f8")] + [(signal_name, "f8") for signal_name in signal_names]
            data = np.empty(len(rows), dtype=dtype)
            for index, row in enumerate(rows):
                data[index]["timestamp"] = float(row["timestamp"])
                for signal_name in signal_names:
                    data[index][signal_name] = float(row.get(signal_name, np.nan))
            channel.create_dataset(message_name, data=data, compression="gzip")

    return h5_path, {"total_frames": total_frames, "decoded_frames": decoded_frames}


def to_float_or_nan(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return float("nan")


def add_can_code_to_path() -> None:
    code_root = str(CAN_CODE_ROOT)
    if code_root not in sys.path:
        sys.path.insert(0, code_root)


def list_metric_files() -> list[Path]:
    if not METRICS_DIR.exists():
        return []
    return sorted(path for path in METRICS_DIR.glob("*.py") if not path.name.startswith("__"))


def normalize_problem_groups(selected_groups: list[str] | None) -> list[str]:
    groups = list(selected_groups or DEFAULT_PROBLEM_GROUPS)
    normalized = [group for group in groups if group in PROBLEM_GROUP_DEFINITIONS]
    return normalized or list(DEFAULT_PROBLEM_GROUPS)


def infer_problem_groups_from_text(text: str) -> list[str]:
    value = (text or "").lower()
    groups: list[str] = []
    if any(token in value for token in ["abs", "tcs", "vse", "稳定", "附着", "车身稳定"]):
        groups.append("stability_control")
    if any(
        token in value
        for token in [
            "acc",
            "跟车",
            "纵向",
            "重刹",
            "重制动",
            "急减速",
            "急刹",
            "误制动",
            "误刹",
            "制动",
            "setspeed",
            "set speed",
            "设速",
            "稳速",
            "不合理低速",
            "低速异常",
            "溜车",
            "回退",
        ]
    ):
        groups.append("acc_control")
    return [group for group in groups if group in PROBLEM_GROUP_DEFINITIONS]


def selected_metric_names(selected_groups: list[str] | None) -> set[str]:
    metrics: set[str] = set()
    for group in normalize_problem_groups(selected_groups):
        metrics.update(PROBLEM_GROUP_DEFINITIONS[group]["metrics"])
    return metrics


def list_problem_groups() -> list[dict[str, str]]:
    return [
        {
            "key": key,
            "label": str(config["label"]),
            "description": str(config["description"]),
        }
        for key, config in PROBLEM_GROUP_DEFINITIONS.items()
    ]


def ordered_metric_names(selected_groups: list[str] | None = None) -> list[str]:
    allowed = selected_metric_names(selected_groups)
    return [metric_name for metric_name in METRIC_DESCRIPTIONS if metric_name in allowed]


def metric_group_keys(metric_name: str) -> list[str]:
    keys: list[str] = []
    for group_key, config in PROBLEM_GROUP_DEFINITIONS.items():
        if metric_name in config["metrics"]:
            keys.append(group_key)
    return keys


def metric_group_labels(metric_name: str) -> list[str]:
    labels: list[str] = []
    for config in PROBLEM_GROUP_DEFINITIONS.values():
        if metric_name in config["metrics"]:
            labels.append(str(config["label"]))
    return labels


def split_preset_cell(value: str) -> tuple[str, ...]:
    normalized = re.sub(r"[；;、\r\n]+", ";", str(value or ""))
    return tuple(part.strip() for part in normalized.split(";") if part.strip())


PRESET_SIGNAL_NOTE_PATTERN = re.compile(
    r"[（(][^（）()]*?(?:待补充|待核对|候选|部分匹配|缺失|TODO|todo|pending)[^（）()]*[）)]$"
)


def clean_preset_signal_name(name: str) -> str:
    value = str(name or "").strip().strip("`")
    value = PRESET_SIGNAL_NOTE_PATTERN.sub("", value).strip()
    return value


def split_preset_signal_cell(value: str) -> tuple[str, ...]:
    signals: list[str] = []
    for part in split_preset_cell(value):
        signal = clean_preset_signal_name(part)
        if signal and signal not in signals:
            signals.append(signal)
    return tuple(signals)


def performance_problem_preset_key(label: str) -> str:
    return normalize_metric_query(label) or safe_filename(label).lower()


def _xlsx_column_index(cell_ref: str) -> int:
    letters = re.match(r"([A-Z]+)", cell_ref or "")
    if not letters:
        return 0
    index = 0
    for char in letters.group(1):
        index = index * 26 + ord(char) - ord("A") + 1
    return max(index - 1, 0)


def _xlsx_text(element: ET.Element) -> str:
    return "".join(text for text in element.itertext() if text is not None)


def _xlsx_first_sheet_path(archive: zipfile.ZipFile) -> str:
    ns = {
        "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        "pkg": "http://schemas.openxmlformats.org/package/2006/relationships",
    }
    workbook_root = ET.fromstring(archive.read("xl/workbook.xml"))
    first_sheet = workbook_root.find("main:sheets/main:sheet", ns)
    if first_sheet is None:
        return "xl/worksheets/sheet1.xml"
    relation_id = first_sheet.attrib.get(f"{{{ns['rel']}}}id")
    if not relation_id:
        return "xl/worksheets/sheet1.xml"

    rels_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    for rel in rels_root.findall("pkg:Relationship", ns):
        if rel.attrib.get("Id") != relation_id:
            continue
        target = rel.attrib.get("Target") or "worksheets/sheet1.xml"
        if target.startswith("/"):
            return target.lstrip("/")
        return "xl/" + target.lstrip("/")
    return "xl/worksheets/sheet1.xml"


def _read_xlsx_rows(path: Path) -> list[list[str]]:
    ns = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as archive:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared_strings = [_xlsx_text(item) for item in shared_root.findall("main:si", ns)]

        sheet_root = ET.fromstring(archive.read(_xlsx_first_sheet_path(archive)))
        parsed_rows: list[list[str]] = []
        for row in sheet_root.findall(".//main:sheetData/main:row", ns):
            values_by_col: dict[int, str] = {}
            for cell in row.findall("main:c", ns):
                col_index = _xlsx_column_index(cell.attrib.get("r", ""))
                cell_type = cell.attrib.get("t")
                value_node = cell.find("main:v", ns)
                inline_node = cell.find("main:is", ns)
                if cell_type == "s" and value_node is not None:
                    value_index = safe_int(value_node.text or 0)
                    value = shared_strings[value_index] if 0 <= value_index < len(shared_strings) else ""
                elif cell_type == "inlineStr" and inline_node is not None:
                    value = _xlsx_text(inline_node)
                elif value_node is not None:
                    value = value_node.text or ""
                else:
                    value = ""
                values_by_col[col_index] = str(value).strip()
            if values_by_col:
                width = max(values_by_col) + 1
                parsed_rows.append([values_by_col.get(index, "") for index in range(width)])
    return parsed_rows


def load_performance_problem_presets() -> dict[str, PerformanceProblemPreset]:
    if not PERFORMANCE_PRESET_TABLE_PATH.exists():
        return {}

    rows = _read_xlsx_rows(PERFORMANCE_PRESET_TABLE_PATH)
    if not rows:
        return {}
    headers = [str(cell or "").strip() for cell in rows[0]]
    column_index = {name: index for index, name in enumerate(headers)}
    if any(column not in column_index for column in PERFORMANCE_PRESET_REQUIRED_COLUMNS):
        return {}

    presets: dict[str, PerformanceProblemPreset] = {}
    for values in rows[1:]:
        row = {
            column: (
                values[column_index[column]]
                if column in column_index and column_index[column] < len(values)
                else ""
            )
            for column in PERFORMANCE_PRESET_COLUMNS
        }
        label = str(row.get("问题类型") or "").strip()
        if not label:
            continue
        key = performance_problem_preset_key(label)
        aliases = tuple(dict.fromkeys((label, *split_preset_cell(str(row.get("触发词") or "")))))
        presets[key] = PerformanceProblemPreset(
            key=key,
            label=label,
            aliases=aliases,
            vehicle_signals=split_preset_signal_cell(str(row.get("实车表现关注信号") or "")),
            algorithm_request_signals=split_preset_signal_cell(str(row.get("算法请求关注信号") or "")),
            actuator_request_signals=split_preset_signal_cell(str(row.get("执行器请求关注信号") or "")),
            metrics=split_preset_cell(str(row.get("关联问题筛选代码") or "")),
            algorithm_cause_signals=split_preset_signal_cell(str(row.get("算法请求诱因关注信号") or "")),
            algorithm_cause_logic=str(row.get("算法请求诱因判断逻辑") or "").strip(),
            root_cause_output_hint=str(row.get("行车问题原因分析输出要求") or "").strip(),
            note=str(row.get("说明") or "").strip(),
        )
    return presets


def performance_problem_preset_signal_short_names() -> tuple[str, ...]:
    signal_names: set[str] = set()
    for preset in load_performance_problem_presets().values():
        for signal in (
            *preset.vehicle_signals,
            *preset.algorithm_request_signals,
            *preset.actuator_request_signals,
            *preset.algorithm_cause_signals,
        ):
            short_name = _canonical_signal_short_name(clean_preset_signal_name(signal))
            if short_name:
                signal_names.add(short_name)
    return tuple(sorted(signal_names))


def root_cause_extra_signal_short_names() -> tuple[str, ...]:
    return tuple(
        sorted(
            set(ROOT_CAUSE_EXTRA_SIGNAL_SHORT_NAMES)
            | set(performance_problem_preset_signal_short_names())
        )
    )


def performance_preset_table_warnings() -> list[str]:
    warnings: list[str] = []
    if not PERFORMANCE_PRESET_TABLE_PATH.exists():
        return [f"性能问题预设表不存在：{PERFORMANCE_PRESET_TABLE_PATH.relative_to(ROOT)}"]

    try:
        rows = _read_xlsx_rows(PERFORMANCE_PRESET_TABLE_PATH)
        fieldnames = [str(cell or "").strip() for cell in (rows[0] if rows else [])]
    except Exception as exc:
        return [f"性能问题预设表读取失败：{exc}"]

    missing_columns = [column for column in PERFORMANCE_PRESET_REQUIRED_COLUMNS if column not in fieldnames]
    if missing_columns:
        return ["性能问题预设表缺少列：" + "、".join(missing_columns)]

    presets = load_performance_problem_presets()
    if not presets:
        return ["性能问题预设表没有有效问题类型配置。"]

    for preset in presets.values():
        if not preset.metrics:
            warnings.append(f"性能问题预设 `{preset.label}` 未配置关联问题筛选代码。")
            continue
        unknown_metrics = [metric for metric in preset.metrics if metric not in METRIC_DESCRIPTIONS]
        if unknown_metrics:
            warnings.append(
                f"性能问题预设 `{preset.label}` 关联了未注册指标：{', '.join(unknown_metrics)}"
            )
    return warnings


def infer_performance_problem_preset_keys(text: str) -> list[str]:
    normalized_text = normalize_metric_query(text or "")
    if not normalized_text:
        return []

    matched: list[str] = []
    for key, preset in load_performance_problem_presets().items():
        for alias in preset.aliases:
            normalized_alias = normalize_metric_query(alias)
            if normalized_alias and normalized_alias in normalized_text:
                matched.append(key)
                break
    return matched


def metrics_for_performance_problem_presets(preset_keys: list[str]) -> tuple[list[str], list[str], list[str]]:
    presets = load_performance_problem_presets()
    metrics: list[str] = []
    labels: list[str] = []
    notes: list[str] = []
    for key in preset_keys:
        preset = presets.get(key)
        if not preset:
            continue
        labels.append(preset.label)
        if preset.note:
            notes.append(f"{preset.label}：{preset.note}")
        for metric in preset.metrics:
            if metric in METRIC_DESCRIPTIONS and metric not in metrics:
                metrics.append(metric)
    return metrics, labels, notes


def resolve_performance_analysis_scope(
    request_text: str,
    selected_groups: list[str] | None = None,
) -> PerformanceAnalysisScope:
    notes = performance_preset_table_warnings()
    preset_keys = infer_performance_problem_preset_keys(request_text)
    if preset_keys:
        metrics, labels, preset_notes = metrics_for_performance_problem_presets(preset_keys)
        groups: list[str] = []
        for metric in metrics:
            for group_key in metric_group_keys(metric):
                if group_key not in groups:
                    groups.append(group_key)
        return PerformanceAnalysisScope(
            problem_types=tuple(preset_keys),
            problem_labels=tuple(labels),
            groups=tuple(groups or normalize_problem_groups(selected_groups)),
            metrics=tuple(metrics),
            notes=tuple(notes + preset_notes),
        )

    groups = normalize_problem_groups(selected_groups)
    return PerformanceAnalysisScope(
        problem_types=(),
        problem_labels=(),
        groups=tuple(groups),
        metrics=tuple(ordered_metric_names(groups)),
        notes=tuple(notes),
    )


def load_performance_metric_knowledge() -> dict[str, dict[str, Any]]:
    global _PERFORMANCE_METRIC_KNOWLEDGE
    if _PERFORMANCE_METRIC_KNOWLEDGE is not None:
        return _PERFORMANCE_METRIC_KNOWLEDGE

    add_can_code_to_path()
    knowledge: dict[str, dict[str, Any]] = {}
    for path in list_metric_files():
        metric_name = path.stem
        title, explanation = METRIC_DESCRIPTIONS.get(metric_name, (humanize_metric_name(metric_name), ""))
        source = read_metric_source(path)
        module, import_error = import_metric_module_for_reference(path)
        required_signals = extract_required_signals(module, source)
        constants = extract_metric_constants(module, source)
        methods = extract_metric_methods(source)
        event_fields = extract_metric_event_fields(source)
        detection_functions = extract_metric_detection_functions(source)
        snippets = extract_metric_rule_snippets(source)
        knowledge[metric_name] = {
            "name": metric_name,
            "title": title,
            "explanation": explanation,
            "groups": metric_group_labels(metric_name),
            "source_file": str(path.relative_to(ROOT)),
            "required_signals": required_signals,
            "constants": constants,
            "methods": methods,
            "event_fields": event_fields,
            "detection_functions": detection_functions,
            "snippets": snippets,
            "import_error": import_error,
        }
    _PERFORMANCE_METRIC_KNOWLEDGE = knowledge
    return knowledge


def read_metric_source(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="gbk", errors="ignore")
    except Exception:
        return ""


def import_metric_module_for_reference(path: Path) -> tuple[object | None, str]:
    try:
        spec = importlib.util.spec_from_file_location(f"agent_metric_reference_{path.stem}", path)
        if spec is None or spec.loader is None:
            return None, "无法加载指标实现模块"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module, ""
    except Exception as exc:
        return None, str(exc)


def extract_required_signals(module: object | None, source: str) -> list[str]:
    if module is not None:
        value = getattr(module, "REQUIRED_SIGNALS", None)
        if isinstance(value, (list, tuple, set)):
            return [str(item) for item in value]

    names = {
        name: match.group("value")
        for name, match in (
            (m.group("name"), m)
            for m in re.finditer(r'(?P<name>[A-Z0-9_]+_SIGNAL)\s*=\s*["\'](?P<value>[^"\']+)["\']', source)
        )
    }
    block_match = re.search(r"REQUIRED_SIGNALS\s*=\s*\[(?P<body>.*?)\]", source, flags=re.DOTALL)
    if not block_match:
        return []
    signals: list[str] = []
    for token in re.findall(r'["\']([^"\']+)["\']|([A-Z0-9_]+_SIGNAL)', block_match.group("body")):
        literal, name = token
        if literal:
            signals.append(literal)
        elif name and name in names:
            signals.append(names[name])
    return signals


def extract_metric_constants(module: object | None, source: str) -> dict[str, str]:
    constants: dict[str, str] = {}
    if module is not None:
        for name, value in sorted(vars(module).items()):
            if not should_expose_metric_constant(name, value):
                continue
            constants[name] = compact_metric_value(value)

    if constants:
        return constants

    pattern = re.compile(
        r"^(?P<name>(?:DEFAULT|STATE|ACC|ABS|TCS|VSE|FORWARD|REVERSE|HOLD|PARKING|ARBITRATION)[A-Z0-9_]*(?:VALUES|THRESHOLD|SECONDS|WINDOW|COUNT|AMPLITUDE|DEADBAND|SLOPE|DROP|DURATION)?)\s*=\s*(?P<value>.+)$",
        flags=re.MULTILINE,
    )
    for match in pattern.finditer(source):
        name = match.group("name")
        value = match.group("value").strip()
        if len(value) <= 120:
            constants[name] = value
    return constants


def should_expose_metric_constant(name: str, value: Any) -> bool:
    if name.startswith("_"):
        return False
    interesting = (
        name.startswith("DEFAULT_")
        or name.endswith("_VALUES")
        or name.endswith("_THRESHOLD")
        or name.endswith("_SECONDS")
        or name.endswith("_WINDOW")
        or name.endswith("_COUNT")
        or name.endswith("_AMPLITUDE")
        or name.endswith("_DEADBAND")
        or name.endswith("_SLOPE")
        or name.endswith("_DROP")
        or name.endswith("_DURATION")
    )
    if not interesting:
        return False
    return isinstance(value, (str, int, float, bool, list, tuple, set))


def compact_metric_value(value: Any, limit: int = 80) -> str:
    if isinstance(value, set):
        value = sorted(value)
    elif isinstance(value, tuple):
        value = list(value)
    text = repr(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def extract_metric_methods(source: str) -> list[str]:
    methods = re.findall(r'["\']method["\']\s*:\s*["\']([^"\']+)["\']', source)
    return unique_preserve_order(methods)[:4]


def extract_metric_event_fields(source: str) -> list[str]:
    fields: list[str] = []
    for match in re.finditer(r"event\[['\"]([^'\"]+)['\"]\]|event\.get\(['\"]([^'\"]+)['\"]", source):
        fields.append(match.group(1) or match.group(2))
    return unique_preserve_order(fields)[:12]


def extract_metric_detection_functions(source: str) -> list[str]:
    names = re.findall(r"^def\s+([a-zA-Z_][a-zA-Z0-9_]*(?:detect|count|event|scene)[a-zA-Z0-9_]*)\s*\(", source, flags=re.MULTILINE)
    names += re.findall(r"^def\s+(_detect_[a-zA-Z0-9_]+)\s*\(", source, flags=re.MULTILINE)
    return unique_preserve_order(names)[:5]


def extract_metric_rule_snippets(source: str, limit: int = 6) -> list[str]:
    snippets: list[str] = []
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if (
            "valid_mask" in line
            or "_matches_enum" in line
            or "threshold" in line.lower()
            or "DEFAULT_" in line
            or "event_count" in line
            or "qualified" in line
        ):
            snippets.append(line)
        if len(snippets) >= limit:
            break
    return snippets


def unique_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = str(value).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def wants_metric_table(question: str) -> bool:
    value = question or ""
    return any(token in value for token in ["表", "列表", "总的", "总表", "全部", "所有", "每个指标", "有哪些指标"])


def wants_metric_rule_detail(question: str) -> bool:
    value = (question or "").lower()
    return any(
        token in value
        for token in [
            "规则",
            "阈值",
            "怎么算",
            "怎么判",
            "判定",
            "逻辑",
            "条件",
            "需要哪些信号",
            "关键信号",
            "信号",
            "代码",
            "源码",
            "实现",
            "伪代码",
            "指标说明",
            "具体规则",
        ]
    )


def select_metrics_for_performance_query(question: str, selected_groups: list[str] | None) -> list[str]:
    preset_keys = infer_performance_problem_preset_keys(question)
    if preset_keys:
        metrics, _, _ = metrics_for_performance_problem_presets(preset_keys)
        candidate_names = metrics
    else:
        inferred = infer_problem_groups_from_text(question)
        if inferred:
            groups = inferred
        elif selected_groups:
            groups = normalize_problem_groups(selected_groups)
        else:
            groups = list(DEFAULT_PROBLEM_GROUPS)
        candidate_names = ordered_metric_names(groups)

    if not candidate_names:
        candidate_names = ordered_metric_names(selected_groups)

    if wants_metric_table(question) and not wants_metric_rule_detail(question):
        return candidate_names

    knowledge = load_performance_metric_knowledge()
    matched = [
        metric_name
        for metric_name in candidate_names
        if performance_metric_matches_query(question, knowledge.get(metric_name, {}))
    ]
    if matched:
        return matched
    return candidate_names


def groups_for_performance_query(question: str, selected_groups: list[str] | None) -> list[str]:
    preset_keys = infer_performance_problem_preset_keys(question)
    if preset_keys:
        metrics, _, _ = metrics_for_performance_problem_presets(preset_keys)
        groups: list[str] = []
        for metric in metrics:
            for group_key in metric_group_keys(metric):
                if group_key not in groups:
                    groups.append(group_key)
        if groups:
            return groups
    inferred = infer_problem_groups_from_text(question)
    if inferred:
        groups = inferred
    elif selected_groups:
        groups = normalize_problem_groups(selected_groups)
    else:
        groups = list(DEFAULT_PROBLEM_GROUPS)
    return groups


def performance_metric_matches_query(question: str, item: dict[str, Any]) -> bool:
    value = (question or "").lower()
    if not value:
        return False
    searchable_parts = [
        str(item.get("name") or ""),
        str(item.get("title") or ""),
        str(item.get("explanation") or ""),
        " ".join(str(group) for group in item.get("groups") or []),
        " ".join(str(signal) for signal in item.get("required_signals") or []),
        " ".join(str(method) for method in item.get("methods") or []),
        " ".join(str(key) for key in (item.get("constants") or {}).keys()),
    ]
    blob = " ".join(searchable_parts).lower()
    normalized_blob = normalize_metric_query(blob)
    normalized_query = normalize_metric_query(value)
    title_norm = normalize_metric_query(str(item.get("title") or ""))
    name_norm = normalize_metric_query(str(item.get("name") or ""))
    if title_norm and title_norm in normalized_query:
        return True
    if name_norm and name_norm in normalized_query:
        return True

    score = 0
    broad_tokens = {"acc", "abs", "tcs", "vse", "性能", "performance", "metric", "指标", "规则", "阈值", "信号"}
    for token in re.findall(r"[a-zA-Z0-9_]+", value):
        if len(token) < 3 or token in broad_tokens:
            continue
        if token in normalized_blob:
            score += 2

    chinese_terms = [
        "急减速",
        "重刹",
        "重制动",
        "加速度",
        "抖动",
        "接管",
        "释放",
        "制动请求",
        "起步",
        "回退",
        "设定速度",
        "控制权",
        "仲裁",
        "丢失",
        "泊车",
        "换挡",
        "顿挫",
        "溜车",
        "完成",
        "激活",
        "牵引力",
        "稳定",
        "附着",
        "ABS",
        "TCS",
        "VSE",
    ]
    for term in chinese_terms:
        normalized_term = normalize_metric_query(term)
        if normalized_term and normalized_term in normalized_query and normalized_term in normalized_blob:
            score += 1

    return score >= 2


def normalize_metric_query(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (value or "").lower())


def summarize_metric_rule(item: dict[str, Any]) -> str:
    explanation = str(item.get("explanation") or "").strip()
    if explanation:
        return explanation
    methods = item.get("methods") or []
    constants = item.get("constants") or {}
    if methods:
        return str(methods[0]).replace("_", " ")
    if constants:
        keys = list(constants)[:3]
        return "主要看 " + "、".join(keys)
    return str(item.get("explanation") or "按指标实现中的事件规则识别")


def summarize_metric_signals(item: dict[str, Any], limit: int = 3) -> str:
    signals = list(item.get("required_signals") or [])
    if not signals:
        return "未声明"
    head = [short_signal_name(signal) for signal in signals[:limit]]
    suffix = f" 等 {len(signals)} 个" if len(signals) > limit else ""
    return "、".join(head) + suffix


def summarize_metric_constants(item: dict[str, Any], limit: int = 4) -> str:
    constants = item.get("constants") or {}
    if not constants:
        return "无显式阈值"
    pairs = [f"{key}={value}" for key, value in list(constants.items())[:limit]]
    suffix = " ..." if len(constants) > limit else ""
    return "；".join(pairs) + suffix


def short_signal_name(signal: str) -> str:
    return str(signal).split(".")[-1]


def format_metric_table(metric_names: list[str], knowledge: dict[str, dict[str, Any]]) -> list[str]:
    lines = [
        "| 功能范围 | 指标 | 规则摘要 | 关键输入 | 主要阈值/枚举 |",
        "|---|---|---|---|---|",
    ]
    for metric_name in metric_names:
        item = knowledge.get(metric_name, {})
        lines.append(
            "| "
            + " | ".join(
                [
                    "、".join(item.get("groups") or []),
                    str(item.get("title") or metric_name),
                    summarize_metric_rule(item),
                    summarize_metric_signals(item),
                    summarize_metric_constants(item),
                ]
            )
            + " |"
        )
    return lines


def wants_problem_preset_table(question: str) -> bool:
    value = question or ""
    normalized = normalize_metric_query(value)
    if any(token in value for token in ["问题类型", "关注信号", "算法请求", "执行器请求", "筛选代码", "预设表", "诱因", "归因"]):
        return True
    if any(
        token in normalized
        for token in [
            "顿挫",
            "重制动",
            "误制动",
            "漏制动",
            "没制动",
            "制动没响应",
            "溜车",
            "不合理低速",
            "跟车距离",
            "setspeed",
        ]
    ):
        return any(token in value for token in ["表", "哪些", "规则", "信号", "怎么", "代码"])
    return False


def format_performance_problem_preset_table(preset_keys: list[str] | None = None) -> list[str]:
    presets = load_performance_problem_presets()
    if preset_keys:
        selected = [presets[key] for key in preset_keys if key in presets]
    else:
        selected = list(presets.values())
    lines = [
        "| 问题类型 | 实车表现关注信号 | 算法请求关注信号 | 执行器请求关注信号 | 算法请求诱因关注信号 | 关联问题筛选代码 |",
        "|---|---|---|---|---|---|",
    ]
    for preset in selected:
        lines.append(
            "| "
            + " | ".join(
                [
                    preset.label,
                    "、".join(preset.vehicle_signals) or "待补充",
                    "、".join(preset.algorithm_request_signals) or "待补充",
                    "、".join(preset.actuator_request_signals) or "待补充",
                    "、".join(preset.algorithm_cause_signals) or "待补充",
                    "、".join(preset.metrics) or "待补充",
                ]
            )
            + " |"
        )
    return lines


def build_metric_pseudocode(item: dict[str, Any]) -> list[str]:
    method = ", ".join(item.get("methods") or item.get("detection_functions") or ["指标事件规则"])
    signals = summarize_metric_signals(item, limit=4)
    constants = summarize_metric_constants(item, limit=3)
    fields = "、".join(str(field) for field in (item.get("event_fields") or [])[:5]) or "start/end/duration/severity"
    return [
        f"读取并对齐关键输入信号：{signals}",
        "筛选处于目标功能/控制状态下的有效样本，过滤驾驶员接管或场景不满足的片段",
        f"按 `{method}` 检查阈值、枚举或状态跳变；主要参数：{constants}",
        f"将连续命中的样本合并成事件窗口，并输出 {fields} 等字段",
    ]


def format_metric_detail(metric_name: str, item: dict[str, Any]) -> list[str]:
    constants = item.get("constants") or {}
    lines = [
        f"**{item.get('title') or metric_name}**",
        f"- 归属：{'、'.join(item.get('groups') or []) or '未归类'}",
        f"- 指标含义：{item.get('explanation') or '暂无说明'}",
        "- 知识来源：指标说明 + 本地指标实现",
        f"- 判定方法：{', '.join(item.get('methods') or item.get('detection_functions') or ['按指标实现事件规则识别'])}",
        f"- 关键输入：{', '.join(f'`{signal}`' for signal in (item.get('required_signals') or [])[:8]) or '未声明'}",
    ]
    if constants:
        lines.append("- 主要阈值/枚举：")
        for key, value in list(constants.items())[:10]:
            lines.append(f"  - `{key}` = `{value}`")
    if item.get("event_fields"):
        lines.append("- 事件输出字段：" + "、".join(f"`{field}`" for field in item["event_fields"][:10]))
    lines.append("- 伪代码逻辑：")
    for index, step in enumerate(build_metric_pseudocode(item), start=1):
        lines.append(f"  {index}. {step}")
    if item.get("import_error"):
        lines.append(f"- 备注：指标实现可读取，但模块导入失败：`{item['import_error']}`")
    return lines


def answer_performance_capability_question(
    question: str,
    selected_groups: list[str] | None = None,
) -> str:
    knowledge = load_performance_metric_knowledge()
    metric_names = select_metrics_for_performance_query(question, selected_groups)
    groups = groups_for_performance_query(question, selected_groups)
    preset_keys = infer_performance_problem_preset_keys(question)
    _, preset_labels, _ = metrics_for_performance_problem_presets(preset_keys)
    show_preset_table = wants_problem_preset_table(question)
    show_table = wants_metric_table(question) or len(metric_names) > 5
    show_detail = wants_metric_rule_detail(question) and len(metric_names) <= 5

    lines = ["性能指标知识库命中："]
    lines.append(f"- 覆盖范围：{format_problem_group_scope(groups)}")
    if preset_labels:
        lines.append(f"- 问题类型：{'、'.join(preset_labels)}")
    lines.append(f"- 指标数量：{len(metric_names)} / {len(METRIC_DESCRIPTIONS)}")
    lines.append("- 数据文件：支持 `.blf`、`.h5`、`.hdf5`；不指定问题范围时默认全跑。")
    lines.append("")

    if show_preset_table:
        lines.append("**问题类型预设表**")
        lines.extend(format_performance_problem_preset_table(preset_keys or None))
        lines.append("")

    if show_table:
        lines.append("**指标总览**")
        lines.extend(format_metric_table(metric_names, knowledge))
        lines.append("")

    if show_detail:
        lines.append("**指标规则细节**")
        for metric_name in metric_names:
            lines.extend(format_metric_detail(metric_name, knowledge.get(metric_name, {})))
            lines.append("")
    elif wants_metric_rule_detail(question):
        lines.append("**规则摘要**")
        for metric_name in metric_names:
            item = knowledge.get(metric_name, {})
            lines.append(
                f"- **{item.get('title') or metric_name}**：{summarize_metric_rule(item)}；"
                f"关键输入：{summarize_metric_signals(item)}；"
                f"主要阈值：{summarize_metric_constants(item, limit=3)}"
            )
        lines.append("")

    if not show_table and not wants_metric_rule_detail(question):
        for group in normalize_problem_groups(groups):
            config = PROBLEM_GROUP_DEFINITIONS.get(group)
            if not config:
                continue
            lines.append(f"**{config['label']}**")
            for metric_name in ordered_metric_names([group]):
                item = knowledge.get(metric_name, {})
                lines.append(f"- **{item.get('title') or metric_name}**：{item.get('explanation') or ''}")
            lines.append("")

    lines.extend(
        [
            "如果你想看某个指标的完整规则，可以直接问：`ACC 急减速/重刹的判定规则是什么`。",
            "准备实际分析时，直接说清关注范围并上传数据文件即可。",
        ]
    )
    return "\n".join(lines).strip()


def problem_group_label(group_key: str) -> str:
    item = PROBLEM_GROUP_DEFINITIONS.get(group_key)
    return str(item["label"]) if item else group_key


def load_metric_modules(
    selected_groups: list[str] | None = None,
    selected_metrics: list[str] | None = None,
) -> dict[str, object]:
    add_can_code_to_path()
    modules: dict[str, object] = {}
    allowed_metrics = set(selected_metrics or selected_metric_names(selected_groups))
    for path in list_metric_files():
        if path.stem not in allowed_metrics:
            continue
        spec = importlib.util.spec_from_file_location(f"agent_metric_{path.stem}", path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules[path.stem] = module
    return modules


def parse_events(details: str) -> list[dict[str, float]]:
    events: list[dict[str, float]] = []
    for match in EVENT_PATTERN.finditer(details or ""):
        body = match.group("body")
        start_match = START_PATTERN.search(body)
        end_match = END_PATTERN.search(body)
        if not start_match or not end_match:
            continue
        start = float(start_match.group("value"))
        end = float(end_match.group("value"))
        duration_match = DURATION_PATTERN.search(body)
        events.append(
            {
                "start": start,
                "end": end,
                "duration": float(duration_match.group("value")) if duration_match else max(0.0, end - start),
            }
        )
    return events


def build_issue_plots(session, metric_name: str, module: object, issue: MetricIssue, image_root: Path) -> list[Path]:
    required = list(getattr(module, "REQUIRED_SIGNALS", []) or [])
    if getattr(module, "SIGNAL_CANDIDATES", None):
        resolved_signals = issue.statistics.get("resolved_signals") if isinstance(issue.statistics, dict) else None
        if isinstance(resolved_signals, dict):
            required_keys = list(getattr(module, "REQUIRED_SIGNAL_KEYS", []) or [])
            required = [
                str(resolved_signals[key])
                for key in required_keys
                if key in resolved_signals and resolved_signals[key]
            ]
    if not required:
        return []

    try:
        from manage_bad_cases import build_plot_image, extract_signal_series
    except Exception:
        return []

    try:
        if issue.events:
            start_ts = max(min(event["start"] for event in issue.events) - 5.0, 0.0)
            end_ts = max(event["end"] for event in issue.events) + 5.0
        else:
            start_ts, end_ts = session.common_time_range(required)
        axis_signal = required[0]
        axis, axis_values_raw = session.get_signal_slice(axis_signal, start_ts, end_ts)
        if len(axis) < 2:
            return []

        session._axis_signal_name = axis_signal
        session._axis_values = axis_values_raw
        plot_signals = list(getattr(module, "PLOT_SIGNALS", []) or required)
        if getattr(module, "SIGNAL_CANDIDATES", None):
            statistic_plot_signals = issue.statistics.get("plot_signals") if isinstance(issue.statistics, dict) else None
            if isinstance(statistic_plot_signals, list):
                plot_signals = [str(item) for item in statistic_plot_signals if item]
        signal_series: dict[str, list[float]] = {}
        for signal_name in plot_signals[:12]:
            if signal_name in signal_series:
                continue
            try:
                signal_series[signal_name] = extract_signal_series(session, signal_name, axis, start_ts, end_ts)
            except Exception:
                continue
        if not signal_series:
            return []

        image_root = ensure_writable_dir(image_root)
        image = build_plot_image(
            metric_name,
            Path(session.primary_h5_file).parent.name,
            Path(session.primary_h5_file).name,
            axis,
            signal_series,
            issue.events,
            safe_float(issue.statistics.get("Severity Score", 0.0)),
            "\n".join(issue.details),
        )
        output_path = image_root / f"{metric_name}.png"
        image.save(output_path)
        return [output_path]
    except Exception:
        return []






def safe_int(value: Any) -> int:
    try:
        return int(float(value))
    except Exception:
        return 0


def safe_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def debug_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, indent=2)


def cleanup_analysis_artifacts(uploaded_file: Path | None, result: AnalysisResult | None) -> None:
    targets: set[Path] = set()

    def add_file_parent(path: Path | None) -> None:
        if path is None:
            return
        try:
            resolved = path.resolve()
        except Exception:
            return
        if is_under(resolved, get_upload_dir()) or is_under(resolved, get_result_dir()):
            targets.add(resolved.parent)

    add_file_parent(uploaded_file)
    if result is not None:
        add_file_parent(result.local_file)
        for issue in result.problems:
            for image_path in issue.image_paths:
                add_file_parent(image_path)

    for target in sorted(targets, key=lambda item: len(str(item)), reverse=True):
        safe_remove_artifact_dir(target)


def cleanup_runtime_artifacts() -> None:
    for root in (get_upload_dir(), get_result_dir()):
        try:
            root = ensure_writable_dir(root)
            for child in root.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
        except Exception as exc:
            print(f"清理运行残留目录失败: {root} | {exc}", flush=True)


def is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
        return True
    except Exception:
        return False


def safe_remove_artifact_dir(path: Path) -> None:
    try:
        resolved = path.resolve()
        upload_root = get_upload_dir().resolve()
        result_root = get_result_dir().resolve()
        if resolved in {upload_root, result_root}:
            return
        if not (is_under(resolved, upload_root) or is_under(resolved, result_root)):
            return
        if resolved.is_dir():
            shutil.rmtree(resolved, ignore_errors=True)
    except Exception as exc:
        print(f"清理分析临时目录失败: {path} | {exc}", flush=True)


def format_analysis_markdown_clean(open_id: str, task_label: str, database: str, file_name: str, result: AnalysisResult) -> str:
    display_summary = result.polished_summary or user_friendly_summary(result)
    problem_count = len(result.problems)
    event_count = sum(len(issue.events) for issue in result.problems)
    image_count = sum(len(issue.image_paths) for issue in result.problems)
    top_issues = sorted(
        result.problems,
        key=lambda issue: (
            safe_int(issue.statistics.get("Bad Case", 0)),
            safe_float(issue.statistics.get("Severity Score", 0.0)),
            len(issue.events),
        ),
        reverse=True,
    )

    if result.status != "ok":
        status_color = "red"
        status_title = "分析未完成"
        status_line = display_summary
    elif problem_count:
        status_color = "red"
        status_title = f"发现 {problem_count} 类问题"
        status_line = build_problem_focus(top_issues, event_count)
    else:
        status_color = "green"
        status_title = "无问题"
        status_line = "本段数据未命中当前已定义的性能问题规则。"

    lines = [
        f"**处理对象**：<at id={open_id}></at>",
        f"**工作类型**：{task_label}",
        f"**数据库**：`{database}`",
        f"**上传文件**：`{file_name}`",
        "",
        f"<font color='{status_color}'>**● {status_title}**</font>",
        f"<font color='{status_color}'>**重点结论：{status_line}**</font>",
        "",
        "**▌总结**",
        display_summary,
    ]

    if result.status == "ok":
        lines.extend(
            [
                "",
                "**▌重点概览**",
                f"- 问题类型：<font color='{status_color}'>**{problem_count} 类**</font>",
                f"- 问题时间点：<font color='{status_color}'>**{event_count} 个**</font>",
                f"- 信号图示：<font color='blue'>**{image_count} 张**</font>",
            ]
        )
        if top_issues:
            lines.append(f"- 最需要关注：<font color='red'>**{metric_title(top_issues[0].metric_name)}**</font>")

    if result.warnings:
        lines.extend(["", "**▌处理提示**"])
        lines.extend(f"- {item}" for item in result.warnings[:6])

    lines.extend(["", "**▌出现的问题**"])
    if not top_issues:
        lines.append("- 无。")
    else:
        for issue in top_issues[:10]:
            bad_case = safe_int(issue.statistics.get("Bad Case", 0))
            severity = safe_float(issue.statistics.get("Severity Score", 0.0))
            title = metric_title(issue.metric_name)
            explanation = metric_explanation(issue.metric_name)
            lines.append(
                f"- <font color='red'>**{title}**</font>：触发 **{bad_case}** 次，"
                f"严重度 **{severity:.3f}**，时间点 **{len(issue.events)}** 个"
            )
            lines.append(f"  判断含义：{explanation}")

    lines.extend(["", "**▌问题时间点**"])
    if not top_issues:
        lines.append("- 无。")
    else:
        added = 0
        for issue in top_issues:
            title = metric_title(issue.metric_name)
            if not issue.events:
                lines.append(f"- **{title}**：未返回具体 start/end 时间点。")
                added += 1
                continue
            for event in issue.events[:5]:
                lines.append(
                    f"- **{title}**：<font color='red'>**{event['start']:.3f}s - {event['end']:.3f}s**</font>，"
                    f"持续 {event['duration']:.3f}s"
                )
                added += 1
                if added >= 20:
                    break
            if added >= 20:
                break

    lines.extend(["", "**▌相关总线信号图示**"])
    if image_count:
        lines.append(f"- 已生成 {image_count} 张图示，会随本次结果一起发送。")
        for issue in top_issues:
            if issue.image_paths:
                lines.append(f"- **{metric_title(issue.metric_name)}**：<font color='blue'>**{len(issue.image_paths)} 张**</font>")
    elif result.status == "needs_converter":
        lines.append("- BLF 未完成 H5 转换，暂未生成图示。")
    else:
        lines.append("- 无需生成图示，或本次没有可绘制的问题事件。")

    return "\n".join(lines)


def metric_title(metric_name: str) -> str:
    return METRIC_DESCRIPTIONS.get(metric_name, (humanize_metric_name(metric_name), ""))[0]


def metric_explanation(metric_name: str) -> str:
    return METRIC_DESCRIPTIONS.get(metric_name, ("", "根据对应规则识别到异常场景，需要结合信号图进一步确认。"))[1]


def humanize_metric_name(metric_name: str) -> str:
    return metric_name.replace("_", " ").strip().title() or "未命名问题"


def build_problem_focus(issues: list[MetricIssue], event_count: int) -> str:
    if not issues:
        return "未识别到明确问题。"
    names = [metric_title(issue.metric_name) for issue in issues[:3]]
    if len(issues) > 3:
        problem_text = "、".join(names) + f"等 {len(issues)} 类问题"
    else:
        problem_text = "、".join(names)
    return f"识别到 {problem_text}，共定位 {event_count} 个问题时间点；相关信号图示会随消息发送。"


def format_problem_group_scope(selected_groups: list[str] | None) -> str:
    groups = normalize_problem_groups(selected_groups)
    labels = [problem_group_label(group) for group in groups]
    if set(groups) == set(DEFAULT_PROBLEM_GROUPS):
        return f"全部问题类型（{'、'.join(labels)}）"
    return "、".join(labels)


def user_friendly_summary(result: AnalysisResult) -> str:
    if result.status == "unsupported_file":
        suffix = result.local_file.suffix.lower() if result.local_file else ""
        got = suffix or "无后缀文件"
        return (
            f"文件格式不符合要求：当前性能问题分析支持 `.blf`、`.h5`、`.hdf5`，"
            f"本次收到的是 `{got}`。请重新上传正确格式的文件，我会自动重新开始分析。"
        )

    if result.status == "needs_converter":
        return (
            "BLF 文件已收到，但无法用当前选择的数据库完成解析。常见原因是 BLF 与 ARXML 数据库版本不匹配、"
            "BLF 中没有当前指标需要的消息，或文件内容不是有效 BLF。请确认数据库版本后重新发起分析。"
        )

    if result.status == "missing_dependency":
        return "分析环境缺少必要依赖，当前无法完成解析；请联系维护人员检查 Python 依赖。"

    if result.status == "error":
        return result.summary or "分析执行失败，请检查上传文件、数据库版本和日志。"

    if result.status == "ok" and not result.problems:
        return f"共执行 {len(list_metric_files())} 项性能指标，未识别到已定义的性能问题。"

    if result.status == "ok":
        total_events = sum(len(problem.events) for problem in result.problems)
        top_issues = sorted(
            result.problems,
            key=lambda issue: (
                safe_int(issue.statistics.get("Bad Case", 0)),
                safe_float(issue.statistics.get("Severity Score", 0.0)),
                len(issue.events),
            ),
            reverse=True,
        )
        return (
            f"共执行 {len(list_metric_files())} 项性能规则，"
            f"{build_problem_focus(top_issues, total_events)}"
        )

    return result.summary or "分析已完成。"


def trim_text(value: Any, limit: int = 120) -> str:
    text = str(value or "").strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


