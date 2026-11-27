"""数据总览热力矩阵构建、文件完整度体检与按需打包。

为「数据总览」首页提供：
1. 通用 Excel / CSV / PDF / ZIP 文件内容完整度体检（带 mtime/size 内存缓存）：
   - 识别多 Sheet 中部分 Sheet 仅含表头（0 数据行）；
   - 识别单 Sheet 内多指标列中部分数值列全空或全 0；
   - 读取同目录 ``.file_versions.json`` 获取当前 SHA256 与今日是否发生覆盖更新。
2. 基于已配置任务（公有 / 私有场站）与本地下载目录构建指定日期区间 ``[start, end]``
   的矩阵视图（支持负偏移 ``D-5``、当天 ``D`` 与正偏移 ``D+1`` / ``D+2``）。
3. 把框选的本地最新文件按指定目录结构打包成 ZIP 或复制到目标文件夹。
"""

from __future__ import annotations

import csv
import json
import re
import shutil
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from spider_gd.core.dates import normalize_indicator_windows, read_date_profile
from spider_gd.core.output_paths import sanitize_segment
from spider_gd.core.station_profile import parse_station_profiles
from spider_gd.core.ukey_plan import configured_station_mode, configured_ukey_ids

_DATE_RE = re.compile(r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})")
_MONTH_RE = re.compile(r"(20\d{2})[-_.]?(\d{2})(?!\d)")
_ZERO_TOKENS = {"", "0", "0.0", "0.00", "0.000", "0.0000", "-", "--", "—", "none", "null", "nan"}
_IGNORED_DIRS = {"_versions", "_failures", "_stable_delivery", ".git", "__pycache__"}
_ALLOWED_EXTS = {".xlsx", ".xls", ".csv", ".pdf", ".zip"}

# (path_str, mtime_ns, size) -> inspection dict
_INSPECT_CACHE: dict[tuple[str, int, int], dict[str, Any]] = {}
_MAX_CACHE_ENTRIES = 2048


@dataclass(frozen=True)
class IndexedFile:
    path: Path
    date_str: str
    month_str: str
    name_norm: str
    rel_norm: str
    size: int
    mtime: float


def _is_meaningful_cell(val: Any) -> bool:
    """单元格是否存在非空且非 0 的有效值。"""
    if val is None:
        return False
    if isinstance(val, bool):
        return True
    if isinstance(val, (int, float)):
        return val != 0
    text = str(val).strip().lower()
    if text in _ZERO_TOKENS:
        return False
    try:
        return float(text.replace(",", "")) != 0.0
    except ValueError:
        return True


def _is_non_empty_cell(val: Any) -> bool:
    if val is None:
        return False
    return str(val).strip() != ""


def _read_version_meta(file_path: Path, today: date) -> dict[str, Any]:
    manifest_path = file_path.parent / ".file_versions.json"
    if not manifest_path.exists():
        return {"hash": "", "updated_today": False, "versions_count": 0, "prev_hash": ""}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        item = (manifest.get("files") or {}).get(file_path.name) or {}
    except Exception:
        return {"hash": "", "updated_today": False, "versions_count": 0, "prev_hash": ""}

    cur_hash = str(item.get("current_sha256") or "")[:12]
    updated_at = str(item.get("updated_at") or "")
    versions = item.get("versions") if isinstance(item.get("versions"), list) else []
    prev_hash = str(versions[-1].get("sha256") or "")[:12] if versions and isinstance(versions[-1], dict) else ""
    updated_today = bool(versions) and updated_at.startswith(today.isoformat())
    return {
        "hash": cur_hash,
        "updated_today": updated_today,
        "versions_count": len(versions),
        "prev_hash": prev_hash,
    }


def _inspect_xlsx(path: Path) -> dict[str, Any]:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheet_names = list(wb.sheetnames)
        if not sheet_names:
            return {
                "status": "empty",
                "summary": "工作簿无任何 Sheet",
                "inspect": [{"sheet": "工作簿", "status": "empty", "detail": "0 个 Sheet"}],
            }

        inspect_rows: list[dict[str, str]] = []
        non_empty_sheets = 0
        empty_sheets = 0
        partial_sheets = 0

        for sname in sheet_names:
            ws = wb[sname]
            headers: list[str] = []
            col_non_empty: list[int] = []
            col_meaningful: list[int] = []
            data_rows = 0

            for r_idx, row in enumerate(ws.iter_rows(values_only=True)):
                if r_idx > 600:
                    break
                vals = list(row)
                if not any(_is_non_empty_cell(v) for v in vals):
                    continue
                if not headers:
                    headers = [str(v).strip() if v is not None else f"列{i + 1}" for i, v in enumerate(vals)]
                    col_non_empty = [0] * len(headers)
                    col_meaningful = [0] * len(headers)
                    continue

                data_rows += 1
                if len(vals) > len(col_non_empty):
                    extra = len(vals) - len(col_non_empty)
                    col_non_empty.extend([0] * extra)
                    col_meaningful.extend([0] * extra)
                    headers.extend([f"列{len(headers) + i + 1}" for i in range(extra)])
                for c_idx, cell_val in enumerate(vals):
                    if _is_non_empty_cell(cell_val):
                        col_non_empty[c_idx] += 1
                    if _is_meaningful_cell(cell_val):
                        col_meaningful[c_idx] += 1

            if data_rows == 0:
                empty_sheets += 1
                inspect_rows.append({"sheet": sname, "status": "empty", "detail": "仅含表头，0 行数据"})
                continue

            # 跳过第一列（通常是时刻/日期/序号/场站名），检查其余数据列是否存在全空/全0列
            data_col_indices = list(range(1, len(headers))) if len(headers) > 1 else [0]
            meaningful_cols = [i for i in data_col_indices if col_meaningful[i] > 0]
            empty_cols = [headers[i] for i in data_col_indices if col_meaningful[i] == 0 and headers[i]]

            if not meaningful_cols:
                empty_sheets += 1
                inspect_rows.append({
                    "sheet": sname,
                    "status": "empty",
                    "detail": f"{data_rows} 行骨架存在，但数值列全空或全 0",
                })
            elif empty_cols:
                partial_sheets += 1
                non_empty_sheets += 1
                shown = "、".join(empty_cols[:4]) + (" 等" if len(empty_cols) > 4 else "")
                inspect_rows.append({
                    "sheet": sname,
                    "status": "empty",
                    "detail": f"{len(meaningful_cols)}/{len(data_col_indices)} 列有值；空/全0列：{shown}",
                })
            else:
                non_empty_sheets += 1
                inspect_rows.append({
                    "sheet": sname,
                    "status": "ok",
                    "detail": f"{data_rows} 行有效数据，全部数值列非空",
                })

        total = len(sheet_names)
        if non_empty_sheets == 0:
            return {
                "status": "empty",
                "summary": f"{total} 个 Sheet 均为空表头或全 0",
                "inspect": inspect_rows[:12],
            }
        if empty_sheets > 0 or partial_sheets > 0:
            parts: list[str] = []
            if empty_sheets:
                parts.append(f"{empty_sheets}/{total} 个 Sheet 为空")
            if partial_sheets:
                parts.append(f"{partial_sheets} 个 Sheet 存在空指标列")
            return {
                "status": "partial",
                "summary": "，".join(parts),
                "inspect": inspect_rows[:12],
            }
        return {
            "status": "ok",
            "summary": "",
            "inspect": inspect_rows[:12],
        }
    finally:
        wb.close()


def _inspect_csv(path: Path) -> dict[str, Any]:
    raw_text = ""
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            raw_text = path.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    if not raw_text.strip():
        return {
            "status": "empty",
            "summary": "CSV 文件内容为空",
            "inspect": [{"sheet": path.name, "status": "empty", "detail": "0 行数据"}],
        }
    rows = list(csv.reader(raw_text.splitlines()[:600]))
    if len(rows) <= 1:
        return {
            "status": "empty",
            "summary": "仅含表头（0 行数据）",
            "inspect": [{"sheet": path.name, "status": "empty", "detail": "仅表头"}],
        }
    headers = [c.strip() or f"列{i + 1}" for i, c in enumerate(rows[0])]
    data_rows = [r for r in rows[1:] if any(c.strip() for c in r)]
    if not data_rows:
        return {
            "status": "empty",
            "summary": "仅含表头（0 行数据）",
            "inspect": [{"sheet": path.name, "status": "empty", "detail": "0 行数据"}],
        }
    if len(headers) > 1:
        empty_cols = []
        for idx in range(1, len(headers)):
            if not any(idx < len(r) and _is_meaningful_cell(r[idx]) for r in data_rows):
                empty_cols.append(headers[idx])
        if len(empty_cols) == len(headers) - 1:
            return {
                "status": "empty",
                "summary": "数值列全空或全 0",
                "inspect": [{"sheet": path.name, "status": "empty", "detail": f"{len(data_rows)} 行但数值列全空"}],
            }
        if empty_cols:
            shown = "、".join(empty_cols[:4])
            return {
                "status": "partial",
                "summary": f"存在空指标列：{shown}",
                "inspect": [{"sheet": path.name, "status": "empty", "detail": f"空/全0列：{shown}"}],
            }
    return {
        "status": "ok",
        "summary": "",
        "inspect": [{"sheet": path.name, "status": "ok", "detail": f"{len(data_rows)} 行有效数据"}],
    }


def inspect_file_completeness(path: str | Path, *, today: date | None = None) -> dict[str, Any]:
    """对单个本地数据文件做轻量完整度体检，结果按 (path, mtime_ns, size) 缓存。"""
    p = Path(path)
    today = today or date.today()
    if not p.exists() or not p.is_file():
        return {"status": "miss", "summary": "本地无文件", "inspect": [], "size": 0, "hash": "", "updated_today": False}

    st = p.stat()
    cache_key = (str(p), st.st_mtime_ns, st.st_size)
    cached = _INSPECT_CACHE.get(cache_key)
    if cached is None:
        ext = p.suffix.lower()
        if st.st_size == 0:
            res: dict[str, Any] = {
                "status": "empty",
                "summary": "0 字节空文件",
                "inspect": [{"sheet": p.name, "status": "empty", "detail": "文件大小为 0 B"}],
            }
        elif ext == ".xlsx":
            try:
                res = _inspect_xlsx(p)
            except Exception as exc:
                res = {
                    "status": "partial",
                    "summary": f"表格解析异常：{exc}",
                    "inspect": [{"sheet": p.name, "status": "empty", "detail": str(exc)}],
                }
        elif ext == ".csv":
            try:
                res = _inspect_csv(p)
            except Exception as exc:
                res = {
                    "status": "partial",
                    "summary": f"CSV 解析异常：{exc}",
                    "inspect": [{"sheet": p.name, "status": "empty", "detail": str(exc)}],
                }
        else:
            res = {
                "status": "ok",
                "summary": "",
                "inspect": [{"sheet": p.name, "status": "ok", "detail": f"{st.st_size} 字节"}],
            }
        if len(_INSPECT_CACHE) >= _MAX_CACHE_ENTRIES:
            _INSPECT_CACHE.clear()
        _INSPECT_CACHE[cache_key] = res
        cached = res

    ver = _read_version_meta(p, today)
    return {
        **cached,
        "size": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
        "hash": ver["hash"],
        "prev_hash": ver["prev_hash"],
        "updated_today": ver["updated_today"],
        "versions_count": ver["versions_count"],
        "file_path": str(p),
        "file_name": p.name,
    }


def _extract_date_from_text(text: str) -> tuple[str, str]:
    """从文件名或路径提取 YYYY-MM-DD 与 YYYY-MM。"""
    m = _DATE_RE.search(text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            iso = f"{y:04d}-{mo:02d}-{d:02d}"
            return iso, f"{y:04d}-{mo:02d}"
    mm = _MONTH_RE.search(text)
    if mm:
        y, mo = int(mm.group(1)), int(mm.group(2))
        if 1 <= mo <= 12:
            return "", f"{y:04d}-{mo:02d}"
    return "", ""


def _scan_download_roots(roots: list[Path]) -> list[IndexedFile]:
    files: list[IndexedFile] = []
    seen: set[Path] = set()
    for root in roots:
        if not root or not root.exists():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if p.name.startswith(".") or p.suffix.lower() not in _ALLOWED_EXTS:
                continue
            if any(part in _IGNORED_DIRS for part in p.parts):
                continue
            resolved = p.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            rel = str(p.relative_to(root))
            d_str, m_str = _extract_date_from_text(p.name)
            if not d_str:
                d_str, m_str2 = _extract_date_from_text(rel)
                m_str = m_str or m_str2
            try:
                st = p.stat()
            except OSError:
                continue
            files.append(
                IndexedFile(
                    path=p,
                    date_str=d_str,
                    month_str=m_str,
                    name_norm=p.name.lower(),
                    rel_norm=rel.lower(),
                    size=st.st_size,
                    mtime=st.st_mtime,
                )
            )
    files.sort(key=lambda item: item.mtime, reverse=True)
    return files


def _indicator_keywords(indicator_name: str) -> list[str]:
    """把指标名拆成用于匹配本地文件名的关键词。"""
    cleaned = re.sub(r"^(场站|平台公有数据|现货交易|结算管理)[-/_\s]*", "", indicator_name.strip())
    cleaned = re.sub(r"(查询导出|发电侧导出|数据导出|导出|下载|查询)$", "", cleaned).strip()
    tokens = [t for t in re.split(r"[-/_\s（）()·]+", cleaned) if len(t) >= 2]
    if cleaned and cleaned not in tokens:
        tokens.insert(0, cleaned)
    return [t.lower() for t in tokens]


def _match_file(
    indexed: list[IndexedFile],
    *,
    indicator: str,
    target_date: str,
    date_kind: str,
    station_tokens: list[str],
) -> IndexedFile | None:
    kws = _indicator_keywords(indicator)
    target_month = target_date[:7]
    best: tuple[int, IndexedFile] | None = None

    for f in indexed:
        if date_kind == "month":
            if f.month_str != target_month:
                continue
        else:
            if f.date_str != target_date:
                continue

        if station_tokens and not any(tok in f.rel_norm for tok in station_tokens):
            continue

        score = 0
        for kw in kws:
            if kw in f.name_norm:
                score += 10 + len(kw)
            elif kw in f.rel_norm:
                score += 4 + len(kw)
        if score <= 0:
            continue
        if best is None or score > best[0]:
            best = (score, f)
    return best[1] if best else None


def _shift_month(d: date, delta_months: int) -> tuple[int, int]:
    total = d.year * 12 + (d.month - 1) + delta_months
    return total // 12, (total % 12) + 1


def _format_preview_cell(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%d %H:%M")
    if isinstance(val, date):
        return val.isoformat()
    if isinstance(val, float):
        if val.is_integer():
            return str(int(val))
        return f"{val:.4f}".rstrip("0").rstrip(".")
    return str(val).strip()


def preview_overview_file(
    path: str | Path,
    *,
    sheet: str = "",
    max_rows: int = 80,
    max_cols: int = 24,
) -> dict[str, Any]:
    """轻量读取本地 Excel / CSV 文件前 N 行，用于数据总览抽屉快速预览。"""
    p = Path(path)
    if not p.exists() or not p.is_file():
        return {"ok": False, "kind": "missing", "error": "本地文件不存在", "sheets": [], "headers": [], "rows": []}

    ext = p.suffix.lower()
    if ext not in _ALLOWED_EXTS:
        return {"ok": False, "kind": "unsupported", "error": f"暂不支持预览 {ext or '未知'} 格式", "sheets": [], "headers": [], "rows": []}

    max_rows = max(1, min(int(max_rows), 200))
    max_cols = max(1, min(int(max_cols), 40))

    if ext == ".xlsx":
        import openpyxl

        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        try:
            names = list(wb.sheetnames)
            if not names:
                return {"ok": True, "kind": "table", "file_name": p.name, "active_sheet": "", "sheets": [], "headers": [], "rows": [], "empty_col_indices": [], "total_rows": 0, "truncated": False}

            active_name = sheet if sheet in names else names[0]
            sheets_meta: list[dict[str, Any]] = []
            active_headers: list[str] = []
            active_rows: list[list[str]] = []
            active_empty_cols: list[int] = []
            active_total_rows = 0
            active_truncated = False

            for sname in names:
                ws = wb[sname]
                headers: list[str] = []
                col_meaningful: list[int] = []
                preview_rows: list[list[str]] = []
                data_rows = 0
                is_active = sname == active_name
                scan_limit = 600

                for r_idx, row in enumerate(ws.iter_rows(values_only=True)):
                    if r_idx > scan_limit:
                        if is_active:
                            active_truncated = True
                        break
                    vals = list(row)[:max_cols]
                    if not any(_is_non_empty_cell(v) for v in vals):
                        continue
                    if not headers:
                        headers = [str(v).strip() if v is not None and str(v).strip() else f"列{i + 1}" for i, v in enumerate(vals)]
                        col_meaningful = [0] * len(headers)
                        continue

                    data_rows += 1
                    if len(vals) > len(headers):
                        extra = len(vals) - len(headers)
                        headers.extend([f"列{len(headers) + i + 1}" for i in range(extra)])
                        col_meaningful.extend([0] * extra)
                    for c_idx, cell_val in enumerate(vals):
                        if _is_meaningful_cell(cell_val):
                            col_meaningful[c_idx] += 1
                    if is_active:
                        if len(preview_rows) < max_rows:
                            padded = [_format_preview_cell(v) for v in vals]
                            if len(padded) < len(headers):
                                padded.extend([""] * (len(headers) - len(padded)))
                            preview_rows.append(padded)
                        else:
                            active_truncated = True

                data_col_indices = list(range(1, len(headers))) if len(headers) > 1 else [0]
                empty_indices = [i for i in data_col_indices if i < len(col_meaningful) and col_meaningful[i] == 0]
                meaningful_count = len(data_col_indices) - len(empty_indices)
                if data_rows == 0 or meaningful_count <= 0:
                    st_status = "empty"
                elif empty_indices:
                    st_status = "partial"
                else:
                    st_status = "ok"

                sheets_meta.append({
                    "name": sname,
                    "status": st_status,
                    "rows": data_rows,
                    "empty_cols": [headers[i] for i in empty_indices if i < len(headers)],
                })
                if is_active:
                    active_headers = headers
                    active_rows = preview_rows
                    active_empty_cols = empty_indices
                    active_total_rows = data_rows

            return {
                "ok": True,
                "kind": "table",
                "file_name": p.name,
                "active_sheet": active_name,
                "sheets": sheets_meta,
                "headers": active_headers,
                "rows": active_rows,
                "empty_col_indices": active_empty_cols,
                "total_rows": active_total_rows,
                "truncated": active_truncated,
            }
        finally:
            wb.close()

    if ext == ".csv":
        raw_text = ""
        for enc in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                raw_text = p.read_text(encoding=enc)
                break
            except UnicodeDecodeError:
                continue
        lines = raw_text.splitlines()
        parsed = list(csv.reader(lines[: max_rows + 50]))
        if not parsed:
            return {"ok": True, "kind": "table", "file_name": p.name, "active_sheet": p.name, "sheets": [{"name": p.name, "status": "empty", "rows": 0, "empty_cols": []}], "headers": [], "rows": [], "empty_col_indices": [], "total_rows": 0, "truncated": False}
        headers = [(c.strip() or f"列{i + 1}") for i, c in enumerate(parsed[0][:max_cols])]
        data_rows = [[c.strip() for c in r[:max_cols]] for r in parsed[1:] if any(c.strip() for c in r)]
        data_col_indices = list(range(1, len(headers))) if len(headers) > 1 else [0]
        empty_indices = [
            i for i in data_col_indices
            if not any(i < len(r) and _is_meaningful_cell(r[i]) for r in data_rows)
        ]
        st_status = "empty" if (not data_rows or len(empty_indices) == len(data_col_indices)) else ("partial" if empty_indices else "ok")
        padded_rows = [r + [""] * max(0, len(headers) - len(r)) for r in data_rows[:max_rows]]
        return {
            "ok": True,
            "kind": "table",
            "file_name": p.name,
            "active_sheet": p.name,
            "sheets": [{"name": p.name, "status": st_status, "rows": len(data_rows), "empty_cols": [headers[i] for i in empty_indices]}],
            "headers": headers,
            "rows": padded_rows,
            "empty_col_indices": empty_indices,
            "total_rows": max(len(data_rows), max(0, len(lines) - 1)),
            "truncated": len(lines) - 1 > max_rows,
        }

    return {
        "ok": True,
        "kind": "binary",
        "file_name": p.name,
        "active_sheet": "",
        "sheets": [],
        "headers": [],
        "rows": [],
        "empty_col_indices": [],
        "total_rows": 0,
        "truncated": False,
        "note": f"{ext.upper().lstrip('.')} 文件请点击下方「用本机软件打开」直接查看",
    }


def _build_month_list(start_ym: str, end_ym: str) -> list[str]:
    sy, sm = int(start_ym[:4]), int(start_ym[5:7])
    ey, em = int(end_ym[:4]), int(end_ym[5:7])
    if (ey, em) < (sy, sm):
        sy, sm, ey, em = ey, em, sy, sm
    out: list[str] = []
    y, m = sy, sm
    guard = 0
    while (y, m) <= (ey, em) and guard < 120:
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
        guard += 1
    return out


def build_overview_matrix(
    state_store: Any,
    *,
    default_download_dir: str = "",
    start_date: date,
    end_date: date,
    month_start: str = "",
    month_end: str = "",
    today: date | None = None,
) -> dict[str, Any]:
    """构建数据总览热力矩阵（日度支持 D+1/D+2 与横向无感懒加载，月度按年/月方格返回）。"""
    today = today or date.today()
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    total_days = min(180, (end_date - start_date).days + 1)
    dates = [(start_date + timedelta(days=i)).isoformat() for i in range(total_days)]

    m_start = month_start or f"{today.year - 1:04d}-01"
    m_end = month_end or f"{today.year + 1:04d}-12"
    months = _build_month_list(m_start, m_end)

    # 读取场站配置、任务配置、工作流默认窗口
    raw_stations = ""
    getter = getattr(state_store, "get_setting", None)
    if callable(getter):
        raw_stations = str(getter("stationProfiles") or "")
        configured_dl = str(getter("downloadDir") or "").strip()
    else:
        configured_dl = ""
    dl_root = configured_dl or default_download_dir

    try:
        stations = parse_station_profiles(raw_stations)
    except Exception:
        stations = []

    task_configs = state_store.list_task_configs() if hasattr(state_store, "list_task_configs") else []
    wf_rows = state_store.list_workflow_configs() if hasattr(state_store, "list_workflow_configs") else []
    wf_map: dict[str, dict[str, Any]] = {}
    import yaml

    for row in wf_rows:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        try:
            cfg = yaml.safe_load(row.get("yaml_text") or "") or {}
        except Exception:
            cfg = {}
        wf_map[name] = read_date_profile(cfg)

    # 汇总公有指标与私有指标（分别记录各指标的水位偏移 window_end）
    pub_indicators: dict[str, dict[str, Any]] = {}
    pri_indicators: dict[str, dict[str, Any]] = {}
    roots: list[Path] = []
    if dl_root:
        roots.append(Path(dl_root))

    for task in task_configs:
        custom_dir = str(task.get("download_dir") or "").strip()
        if custom_dir:
            roots.append(Path(custom_dir))
        mode = configured_station_mode(task)
        ukey_ids = configured_ukey_ids(task)
        is_public = mode == "any_one" or ("公有" in str(task.get("name") or "")) or (not ukey_ids and not stations)
        windows = normalize_indicator_windows(task.get("indicator_windows"))
        target_bucket = pub_indicators if is_public else pri_indicators

        for ind in task.get("indicators") or []:
            ind_name = str(ind).strip()
            if not ind_name:
                continue
            prof = wf_map.get(ind_name) or {"kind": "day", "start": -1, "end": -1}
            win = windows.get(ind_name)
            lag_end = int(win[1]) if win else int(prof.get("end", -1))
            date_kind = str(prof.get("kind") or "day")
            target_bucket[ind_name] = {
                "id": ind_name,
                "name": ind_name,
                "lag": lag_end,
                "date_kind": date_kind,
                "task_id": str(task.get("id") or ""),
                "task_name": str(task.get("name") or ""),
            }

    indexed = _scan_download_roots(roots)

    scopes: list[dict[str, Any]] = []
    if pub_indicators:
        scopes.append({"id": "public", "name": "公有数据", "kind": "public", "retention_days": 30})
    if pri_indicators:
        if stations:
            for idx, st in enumerate(stations, start=1):
                scopes.append({
                    "id": st.id,
                    "name": st.name,
                    "ssid": st.wifi_ssid,
                    "slot": idx,
                    "kind": "private",
                    "retention_days": 60,
                })
        else:
            scopes.append({"id": "private_default", "name": "私有数据", "kind": "private", "retention_days": 60})

    matrix_scopes: dict[str, dict[str, Any]] = {}
    for sc in scopes:
        sid = sc["id"]
        is_pub = sc["kind"] == "public"
        ind_list = list((pub_indicators if is_pub else pri_indicators).values())
        st_tokens: list[str] = []
        if not is_pub and sid != "private_default":
            st_tokens = [t.lower() for t in (sc.get("name", ""), sc.get("ssid", "")) if t]

        rows_out: list[dict[str, Any]] = []
        for ind in ind_list:
            lag = int(ind["lag"])
            date_kind = str(ind["date_kind"])
            cells: dict[str, dict[str, Any]] = {}
            if date_kind == "month":
                cutoff_ym = _shift_month(today, lag)
                for mk in months:
                    ym = (int(mk[:4]), int(mk[5:7]))
                    if ym > cutoff_ym:
                        cells[mk] = {"status": "future"}
                        continue
                    matched = _match_file(
                        indexed,
                        indicator=ind["name"],
                        target_date=f"{mk}-01",
                        date_kind="month",
                        station_tokens=st_tokens,
                    )
                    if matched is None:
                        cells[mk] = {
                            "status": "miss",
                            "summary": "已过月度水位截止，本地无该月文件",
                            "inspect": [{"sheet": ind["name"], "status": "miss", "detail": f"本地未找到 {mk} 月度文件"}],
                        }
                    else:
                        cells[mk] = inspect_file_completeness(matched.path, today=today)
            else:
                cutoff_date = today + timedelta(days=lag)
                for d_str in dates:
                    cur_d = date.fromisoformat(d_str)
                    if cur_d > cutoff_date:
                        cells[d_str] = {"status": "future"}
                        continue
                    matched = _match_file(
                        indexed,
                        indicator=ind["name"],
                        target_date=d_str,
                        date_kind=date_kind,
                        station_tokens=st_tokens,
                    )
                    if matched is None:
                        cells[d_str] = {
                            "status": "miss",
                            "summary": "已过水位截止，本地无该日文件",
                            "inspect": [{"sheet": ind["name"], "status": "miss", "detail": "本地未找到对应日期文件"}],
                        }
                    else:
                        cells[d_str] = inspect_file_completeness(matched.path, today=today)
            rows_out.append({
                "id": ind["id"],
                "name": ind["name"],
                "lag": lag,
                "date_kind": date_kind,
                "task_id": ind["task_id"],
                "cells": cells,
            })
        matrix_scopes[sid] = {"rows": rows_out}

    return {
        "today": today.isoformat(),
        "start": dates[0],
        "end": dates[-1],
        "dates": dates,
        "months": months,
        "has_real_config": bool(scopes),
        "download_dir": dl_root,
        "scopes": scopes,
        "data": matrix_scopes,
    }


def iter_pack_overview_files(
    items: list[dict[str, Any]],
    *,
    target_path: str | Path,
    mode: str = "zip",
    layout: str = "station_indicator",
    filename: str = "",
):
    """按文件逐个产出打包/复制进度事件，最终产出 done 汇总结果。"""
    dest = Path(target_path)
    valid: list[tuple[Path, str]] = []
    missing: list[str] = []

    for row in items:
        raw_path = str(row.get("file_path") or "").strip()
        if not raw_path:
            missing.append(f"{row.get('owner', '')} {row.get('indicator', '')} {row.get('date', '')}".strip())
            continue
        src = Path(raw_path)
        if not src.exists() or not src.is_file():
            missing.append(src.name or raw_path)
            continue
        owner = sanitize_segment(str(row.get("owner") or "公有数据"), "数据")
        indicator = sanitize_segment(str(row.get("indicator") or "指标"), "指标")
        day_str = sanitize_segment(str(row.get("date") or ""), "未分类")
        if layout == "station_date":
            arc = f"{owner}/{day_str}/{src.name}"
        elif layout == "flat":
            arc = src.name
        else:
            arc = f"{owner}/{indicator}/{src.name}"
        valid.append((src, arc))

    if not valid:
        yield {
            "event": "done",
            "ok": False,
            "count": 0,
            "missing": missing,
            "output": "",
            "error": "所选格子里没有可导出的本地文件",
        }
        return

    total = len(valid)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    yield {"event": "start", "total": total}

    if mode == "folder":
        out_dir = dest / sanitize_segment(filename, f"电力数据取数包_{ts}") if filename else dest
        out_dir.mkdir(parents=True, exist_ok=True)
        for idx, (src, arc) in enumerate(valid, start=1):
            out_file = out_dir / arc
            out_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out_file)
            yield {
                "event": "progress",
                "done": idx,
                "total": total,
                "percent": round(idx * 100 / total),
                "current_file": src.name,
            }
        yield {
            "event": "done",
            "ok": True,
            "count": total,
            "missing": missing,
            "output": str(out_dir),
            "error": "",
        }
        return

    if dest.suffix.lower() != ".zip":
        zip_name = filename or f"电力数据取数包_{ts}.zip"
        if not zip_name.lower().endswith(".zip"):
            zip_name = f"{zip_name}.zip"
        dest.mkdir(parents=True, exist_ok=True)
        zip_path = dest / zip_name
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        zip_path = dest

    used_names: set[str] = set()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for idx, (src, arc) in enumerate(valid, start=1):
            arc_name = arc
            suffix_n = 1
            while arc_name in used_names:
                p = Path(arc)
                arc_name = str(p.with_name(f"{p.stem}_{suffix_n}{p.suffix}")).replace("\\", "/")
                suffix_n += 1
            used_names.add(arc_name)
            zf.write(src, arcname=arc_name)
            yield {
                "event": "progress",
                "done": idx,
                "total": total,
                "percent": round(idx * 100 / total),
                "current_file": src.name,
            }

    yield {
        "event": "done",
        "ok": True,
        "count": total,
        "missing": missing,
        "output": str(zip_path),
        "error": "",
    }


def pack_overview_files(
    items: list[dict[str, Any]],
    *,
    target_path: str | Path,
    mode: str = "zip",
    layout: str = "station_indicator",
    filename: str = "",
) -> dict[str, Any]:
    """将框选的本地最新文件打包为 ZIP 或导出到目标文件夹。"""
    last: dict[str, Any] = {"ok": False, "count": 0, "missing": [], "output": "", "error": "打包未产出结果"}
    for ev in iter_pack_overview_files(
        items,
        target_path=target_path,
        mode=mode,
        layout=layout,
        filename=filename,
    ):
        if ev.get("event") == "done":
            last = {k: v for k, v in ev.items() if k != "event"}
    return last
