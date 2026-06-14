from __future__ import annotations

import html
import hashlib
import hmac
import io
import json
import os
import re
import sqlite3
import sys
import time
import uuid
import zipfile
from datetime import datetime
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import parse_qs, quote, urlencode, urlparse
from xml.etree import ElementTree as ET


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("UPLOAD_DATA_DIR", BASE_DIR / "data")).resolve()
FILES_DIR = DATA_DIR / "files"
INTERNAL_FILES_DIR = DATA_DIR / "internal_files"
MINERU_RESULTS_DIR = DATA_DIR / "mineru_results"
DB_PATH = DATA_DIR / "uploads.db"
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "30"))
RAW_BASE_PATH = os.environ.get("BASE_PATH", "").strip()
BASE_PATH = "" if RAW_BASE_PATH in {"", "/"} else "/" + RAW_BASE_PATH.strip("/")
UPLOAD_PASSWORD = os.environ.get("UPLOAD_PASSWORD", "20250605")
INTERNAL_PASSWORD = os.environ.get("INTERNAL_PASSWORD", UPLOAD_PASSWORD)
AUTH_SECRET = os.environ.get(
    "AUTH_SECRET",
    f"yinike-material-upload-auth:{UPLOAD_PASSWORD}:{INTERNAL_PASSWORD}",
)
AUTH_MAX_AGE_SECONDS = int(os.environ.get("AUTH_MAX_AGE_SECONDS", str(7 * 24 * 60 * 60)))
UPLOAD_AUTH_SCOPE = "upload"
INTERNAL_AUTH_SCOPE = "internal"
AUTH_COOKIE_NAMES = {
    UPLOAD_AUTH_SCOPE: "yinike_upload_auth",
    INTERNAL_AUTH_SCOPE: "yinike_internal_auth",
}
MINERU_API_KEY = os.environ.get("MINERU_API_KEY", "").strip()
MINERU_FILE_URLS_API = os.environ.get("MINERU_FILE_URLS_API", "https://mineru.net/api/v4/file-urls/batch")
MINERU_BATCH_RESULTS_API = os.environ.get(
    "MINERU_BATCH_RESULTS_API",
    "https://mineru.net/api/v4/extract-results/batch/{batch_id}",
)
MINERU_MODEL_VERSION = os.environ.get("MINERU_MODEL_VERSION", "vlm")
MINERU_LANGUAGE = os.environ.get("MINERU_LANGUAGE", "ch")
MINERU_POLL_SECONDS = float(os.environ.get("MINERU_POLL_SECONDS", "2"))
MINERU_MAX_WAIT_SECONDS = int(os.environ.get("MINERU_MAX_WAIT_SECONDS", "12"))
MINERU_REQUEST_TIMEOUT = int(os.environ.get("MINERU_REQUEST_TIMEOUT", "60"))

DOCUMENT_TYPES = [
    "TDS（产品技术资料）",
    "SDS（安全数据表）",
    "MSDS（化学品安全技术说明书）",
    "COA（检测报告/合格证）",
    "其他",
]
FUNCTION_TYPES = [
    "主剂",
    "固化剂",
    "稀释剂",
    "助剂",
    "底涂",
    "面涂",
    "清洗剂",
    "脱脂剂",
    "防锈剂",
    "表面活化剂",
    "遮蔽/堵孔材料",
    "其他",
]
PROCESS_TYPES = ["喷涂", "浸涂", "清洗", "脱脂", "烘干", "固化", "钝化", "喷砂", "包装", "其他"]
ERP_HEADERS = [
    "序号",
    "物料编号",
    "物料名称",
    "规格型号",
    "单位",
    "类别",
    "供应商编号",
    "最低警戒数",
    "最高警戒数",
]
ERP_CHANGE_FIELDS = [
    ("row_no", "序号"),
    ("material_name", "物料名称"),
    ("spec_model", "规格型号"),
    ("unit", "单位"),
    ("category", "类别"),
    ("supplier_code", "供应商编号"),
    ("min_alert", "最低警戒数"),
    ("max_alert", "最高警戒数"),
]


class ErpConflictError(ValueError):
    def __init__(self, conflicts: list[str]):
        self.conflicts = conflicts
        shown = "；".join(conflicts[:20])
        more = f"；另有 {len(conflicts) - 20} 条未显示" if len(conflicts) > 20 else ""
        super().__init__(
            "ERP 清单与既有记录存在冲突，导入已取消。"
            "请人工核对并修改 ERP 清单或既有记录后再导入。"
            f"冲突项：{shown}{more}"
        )


def column_index_from_cell_ref(cell_ref: str) -> int:
    letters = re.sub(r"[^A-Z]", "", cell_ref.upper())
    index = 0
    for letter in letters:
        index = index * 26 + (ord(letter) - ord("A") + 1)
    return max(index - 1, 0)


def app_url(path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    return f"{BASE_PATH}{path}" if BASE_PATH else path


PAGE_SIZE_OPTIONS = [20, 50, 100]
STATUS_FILTERS = {
    "all": "全部",
    "complete": "已完成",
    "incomplete": "未完成",
}


def normalize_status_filter(value: str) -> str:
    return value if value in STATUS_FILTERS else "all"


def normalize_per_page(value: str | int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 20
    return number if number in PAGE_SIZE_OPTIONS else 20


def normalize_page(value: str | int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 1
    return max(number, 1)


def internal_materials_url(
    query: str = "",
    show_files: str = "",
    status_filter: str = "all",
    page: int = 1,
    per_page: int = 20,
    saved_code: str = "",
) -> str:
    params = {}
    if query:
        params["q"] = query
    status_filter = normalize_status_filter(status_filter)
    if status_filter != "all":
        params["status"] = status_filter
    per_page = normalize_per_page(per_page)
    if per_page != 20:
        params["per_page"] = str(per_page)
    page = normalize_page(page)
    if page > 1:
        params["page"] = str(page)
    if show_files:
        params["show_files"] = show_files
    if saved_code:
        params["saved"] = saved_code
    suffix = f"?{urlencode(params)}" if params else ""
    return f'{app_url("/internal/materials/")}{suffix}'


def route_path(path: str) -> str | None:
    if not BASE_PATH:
        return path
    if path == BASE_PATH:
        return "/"
    if path.startswith(BASE_PATH + "/"):
        return path[len(BASE_PATH):] or "/"
    return None


def cookie_path() -> str:
    return BASE_PATH or "/"


def parse_cookies(cookie_header: str | None) -> dict[str, str]:
    cookies: dict[str, str] = {}
    if not cookie_header:
        return cookies
    for item in cookie_header.split(";"):
        if "=" not in item:
            continue
        key, value = item.strip().split("=", 1)
        if key:
            cookies[key] = value
    return cookies


def sign_auth_expires(scope: str, expires_at: str) -> str:
    return hmac.new(
        AUTH_SECRET.encode("utf-8"),
        f"{scope}:{expires_at}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def make_auth_token(scope: str) -> str:
    expires_at = str(int(time.time()) + AUTH_MAX_AGE_SECONDS)
    return f"{expires_at}.{sign_auth_expires(scope, expires_at)}"


def is_valid_auth_token(scope: str, token: str) -> bool:
    try:
        expires_at, signature = token.split(".", 1)
        expires_int = int(expires_at)
    except ValueError:
        return False
    if expires_int < int(time.time()):
        return False
    return hmac.compare_digest(signature, sign_auth_expires(scope, expires_at))


def auth_cookie_header(scope: str) -> str:
    return (
        f"{AUTH_COOKIE_NAMES[scope]}={make_auth_token(scope)}; "
        f"Max-Age={AUTH_MAX_AGE_SECONDS}; Path={cookie_path()}; HttpOnly; SameSite=Lax"
    )


def auth_scope_for_path(path: str | None) -> str:
    if path and path.startswith("/internal/materials"):
        return INTERNAL_AUTH_SCOPE
    return UPLOAD_AUTH_SCOPE


def password_for_scope(scope: str) -> str:
    if scope == INTERNAL_AUTH_SCOPE:
        return INTERNAL_PASSWORD
    return UPLOAD_PASSWORD


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


MINERU_ENABLE_OCR = env_bool("MINERU_ENABLE_OCR", True)
MINERU_ENABLE_TABLE = env_bool("MINERU_ENABLE_TABLE", True)
MINERU_ENABLE_FORMULA = env_bool("MINERU_ENABLE_FORMULA", False)
MINERU_AUTO_PARSE = env_bool("MINERU_AUTO_PARSE", False)


def ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init_storage() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    INTERNAL_FILES_DIR.mkdir(parents=True, exist_ok=True)
    MINERU_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS uploads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                material_code TEXT NOT NULL,
                supplier TEXT NOT NULL,
                material_function TEXT NOT NULL,
                substrate TEXT NOT NULL,
                process_name TEXT NOT NULL,
                document_type TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                storage_backend TEXT NOT NULL,
                note TEXT,
                uploader_ip TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS erp_materials (
                material_code TEXT PRIMARY KEY,
                row_no TEXT,
                material_name TEXT,
                spec_model TEXT,
                unit TEXT,
                category TEXT,
                supplier_code TEXT,
                min_alert TEXT,
                max_alert TEXT,
                source_filename TEXT,
                active_in_latest_import INTEGER DEFAULT 1,
                erp_change_note TEXT,
                erp_change_detected_at TEXT,
                imported_at TEXT NOT NULL
            )
            """
        )
        ensure_columns(
            conn,
            "erp_materials",
            {
                "active_in_latest_import": "INTEGER DEFAULT 1",
                "erp_change_note": "TEXT",
                "erp_change_detected_at": "TEXT",
            },
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS internal_material_notes (
                material_code TEXT PRIMARY KEY,
                material_usage TEXT,
                process_name TEXT,
                note TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS internal_material_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                material_code TEXT NOT NULL,
                document_type TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                uploader_ip TEXT
            )
            """
        )
        ensure_columns(
            conn,
            "internal_material_files",
            {
                "file_note": "TEXT",
                "mineru_status": "TEXT DEFAULT 'not_submitted'",
                "mineru_batch_id": "TEXT",
                "mineru_data_id": "TEXT",
                "mineru_result_url": "TEXT",
                "mineru_result_dir": "TEXT",
                "mineru_result_json_path": "TEXT",
                "mineru_error": "TEXT",
                "mineru_model_version": "TEXT",
                "mineru_updated_at": "TEXT",
            },
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS erp_import_conflicts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                source_filename TEXT NOT NULL,
                conflict_count INTEGER NOT NULL,
                conflict_text TEXT NOT NULL
            )
            """
        )
        conn.commit()


def safe_segment(value: str, fallback: str = "unknown") -> str:
    value = value.strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1F]+', "_", value)
    value = re.sub(r"\s+", "_", value)
    value = value.strip("._ ")
    return value[:80] or fallback


def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], dict[str, dict[str, bytes | str]]]:
    message_bytes = (
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
        + body
    )
    message = BytesParser(policy=default).parsebytes(message_bytes)
    fields: dict[str, str] = {}
    files: dict[str, dict[str, bytes | str]] = {}

    if not message.is_multipart():
        raise ValueError("上传请求格式错误：不是 multipart/form-data")

    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename:
            files[name] = {"filename": filename, "content": payload}
        else:
            charset = part.get_content_charset() or "utf-8"
            fields[name] = payload.decode(charset, errors="replace").strip()
    return fields, files


def xml_text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return "".join(node.itertext()).strip()


def read_xlsx_rows(content: bytes) -> list[list[str]]:
    ns = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall("a:si", ns):
                shared_strings.append(xml_text(item))

        sheet_names = [name for name in archive.namelist() if name.startswith("xl/worksheets/sheet")]
        if not sheet_names:
            raise ValueError("Excel 文件中没有找到工作表")
        sheet_root = ET.fromstring(archive.read(sorted(sheet_names)[0]))

    rows: list[list[str]] = []
    for row in sheet_root.findall(".//a:sheetData/a:row", ns):
        cells: list[str] = []
        for cell in row.findall("a:c", ns):
            cell_ref = cell.attrib.get("r", "")
            column_index = column_index_from_cell_ref(cell_ref)
            while len(cells) < column_index:
                cells.append("")
            cell_type = cell.attrib.get("t")
            value = ""
            if cell_type == "inlineStr":
                value = xml_text(cell.find("a:is", ns))
            else:
                raw_value = xml_text(cell.find("a:v", ns))
                if cell_type == "s" and raw_value:
                    try:
                        value = shared_strings[int(raw_value)]
                    except (ValueError, IndexError):
                        value = raw_value
                else:
                    value = raw_value
            cells.append(value.strip())
        rows.append(cells)
    return rows


def normalize_erp_rows(rows: list[list[str]]) -> list[dict[str, str]]:
    header_index = -1
    for index, row in enumerate(rows):
        normalized = [cell.strip() for cell in row]
        if "物料编号" in normalized and "物料名称" in normalized:
            header_index = index
            break
    if header_index < 0:
        raise ValueError("没有找到 ERP 表头，请确认包含“物料编号”和“物料名称”列")

    header = [cell.strip() for cell in rows[header_index]]
    positions = {name: header.index(name) for name in ERP_HEADERS if name in header}
    missing = [name for name in ["物料编号", "物料名称"] if name not in positions]
    if missing:
        raise ValueError(f"ERP 表缺少必要列：{', '.join(missing)}")

    materials: list[dict[str, str]] = []
    for row in rows[header_index + 1 :]:
        def get(name: str) -> str:
            position = positions.get(name)
            if position is None or position >= len(row):
                return ""
            return row[position].strip()

        material_code = get("物料编号")
        material_name = get("物料名称")
        if not material_code and not material_name:
            continue
        if not material_code:
            continue
        materials.append(
            {
                "row_no": get("序号"),
                "material_code": material_code,
                "material_name": material_name,
                "spec_model": get("规格型号"),
                "unit": get("单位"),
                "category": get("类别"),
                "supplier_code": get("供应商编号"),
                "min_alert": get("最低警戒数"),
                "max_alert": get("最高警戒数"),
            }
        )
    if not materials:
        raise ValueError("ERP 清单中没有可导入的物料记录")
    return materials


def require_text(fields: dict[str, str], key: str, label: str) -> str:
    value = fields.get(key, "").strip()
    if not value:
        raise ValueError(f"请填写：{label}")
    return value


def get_text(fields: dict[str, str], key: str) -> str:
    return fields.get(key, "").strip()


def resolve_other_choice(fields: dict[str, str], key: str, label: str) -> str:
    value = require_text(fields, key, label)
    if value != "其他":
        return value
    other = require_text(fields, f"{key}_other", f"{label}（其他说明）")
    return f"其他：{other}"


def db_insert(record: dict[str, str]) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO uploads (
                created_at, material_code, supplier, material_function, substrate,
                process_name, document_type, original_filename, stored_path,
                storage_backend, note, uploader_ip
            )
            VALUES (
                :created_at, :material_code, :supplier, :material_function, :substrate,
                :process_name, :document_type, :original_filename, :stored_path,
                :storage_backend, :note, :uploader_ip
            )
            """,
            record,
        )
        conn.commit()
        return int(cursor.lastrowid)


def describe_erp_changes(existing: dict[str, str], incoming: dict[str, str]) -> str:
    changes = []
    for key, label in ERP_CHANGE_FIELDS:
        old_value = str(existing.get(key) or "").strip()
        new_value = str(incoming.get(key) or "").strip()
        if old_value != new_value:
            changes.append(f"{label}: {old_value or '空'} -> {new_value or '空'}")
    return "；".join(changes)


def db_save_erp_conflicts(source_filename: str, conflicts: list[str]) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM erp_import_conflicts")
        conn.execute(
            """
            INSERT INTO erp_import_conflicts (
                created_at, source_filename, conflict_count, conflict_text
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                source_filename,
                len(conflicts),
                "\n".join(conflicts),
            ),
        )
        conn.commit()


def db_clear_erp_conflicts() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM erp_import_conflicts")
        conn.commit()


def db_get_latest_erp_conflict() -> dict[str, str] | None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT created_at, source_filename, conflict_count, conflict_text
            FROM erp_import_conflicts
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    return dict(row) if row else None


def db_sync_erp_materials(materials: list[dict[str, str]], source_filename: str) -> dict[str, int]:
    imported_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        existing_rows = {
            row["material_code"]: dict(row)
            for row in conn.execute(
                """
                SELECT material_code, row_no, material_name, spec_model, unit, category,
                       supplier_code, min_alert, max_alert
                FROM erp_materials
                """
            ).fetchall()
        }
        before_codes = set(existing_rows)
        incoming_codes = {material["material_code"] for material in materials}

        conflicts = []
        for material in materials:
            existing = existing_rows.get(material["material_code"])
            if not existing:
                continue
            change_note = describe_erp_changes(existing, material)
            if change_note:
                conflicts.append(f"{material['material_code']}：{change_note}")
        if conflicts:
            raise ErpConflictError(conflicts)

        conn.execute("UPDATE erp_materials SET active_in_latest_import = 0")
        for material in materials:
            conn.execute(
                """
                INSERT INTO erp_materials (
                    material_code, row_no, material_name, spec_model, unit, category,
                    supplier_code, min_alert, max_alert, source_filename,
                    active_in_latest_import, erp_change_note, erp_change_detected_at, imported_at
                )
                VALUES (
                    :material_code, :row_no, :material_name, :spec_model, :unit, :category,
                    :supplier_code, :min_alert, :max_alert, :source_filename,
                    1, :erp_change_note, :erp_change_detected_at, :imported_at
                )
                ON CONFLICT(material_code) DO UPDATE SET
                    row_no = excluded.row_no,
                    material_name = excluded.material_name,
                    spec_model = excluded.spec_model,
                    unit = excluded.unit,
                    category = excluded.category,
                    supplier_code = excluded.supplier_code,
                    min_alert = excluded.min_alert,
                    max_alert = excluded.max_alert,
                    source_filename = excluded.source_filename,
                    active_in_latest_import = 1,
                    erp_change_note = excluded.erp_change_note,
                    erp_change_detected_at = excluded.erp_change_detected_at,
                    imported_at = excluded.imported_at
                """,
                {
                    **material,
                    "source_filename": source_filename,
                    "erp_change_note": "",
                    "erp_change_detected_at": "",
                    "imported_at": imported_at,
                },
            )
        conn.commit()
    db_clear_erp_conflicts()
    return {
        "total": len(materials),
        "created": len(incoming_codes - before_codes),
        "updated": len(incoming_codes & before_codes),
        "inactive": len(before_codes - incoming_codes),
    }


def db_count_erp_materials() -> int:
    with sqlite3.connect(DB_PATH) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM erp_materials").fetchone()[0])


def internal_materials_base_sql(search_where: str) -> str:
    return f"""
        SELECT
            e.material_code, e.row_no, e.material_name, e.spec_model, e.unit, e.category,
            e.supplier_code, e.min_alert, e.max_alert, e.source_filename, e.imported_at,
            COALESCE(e.active_in_latest_import, 1) AS active_in_latest_import,
            COALESCE(e.erp_change_note, '') AS erp_change_note,
            COALESCE(e.erp_change_detected_at, '') AS erp_change_detected_at,
            COALESCE(n.material_usage, '') AS material_usage,
            COALESCE(n.process_name, '') AS process_name,
            COALESCE(n.note, '') AS note,
            COALESCE(n.updated_at, '') AS note_updated_at,
            COUNT(f.id) AS file_count
        FROM erp_materials e
        LEFT JOIN internal_material_notes n ON n.material_code = e.material_code
        LEFT JOIN internal_material_files f ON f.material_code = e.material_code
        {search_where}
        GROUP BY e.material_code
    """


def completion_filter_sql(status_filter: str) -> str:
    status_filter = normalize_status_filter(status_filter)
    complete_expr = "(material_usage <> '' AND process_name <> '' AND file_count > 0)"
    if status_filter == "complete":
        return f"WHERE {complete_expr}"
    if status_filter == "incomplete":
        return f"WHERE NOT {complete_expr}"
    return ""


def db_count_internal_materials(query: str = "", status_filter: str = "all") -> int:
    pattern = f"%{query.strip()}%"
    search_where = ""
    params: list[str] = []
    if query.strip():
        search_where = "WHERE e.material_code LIKE ? OR e.material_name LIKE ? OR e.spec_model LIKE ?"
        params.extend([pattern, pattern, pattern])
    status_where = completion_filter_sql(status_filter)
    sql = f"""
        WITH material_rows AS (
            {internal_materials_base_sql(search_where)}
        )
        SELECT COUNT(*) FROM material_rows
        {status_where}
    """
    with sqlite3.connect(DB_PATH) as conn:
        return int(conn.execute(sql, params).fetchone()[0])


def db_get_internal_materials(
    query: str = "",
    status_filter: str = "all",
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, str]]:
    pattern = f"%{query.strip()}%"
    search_where = ""
    params: list[str | int] = []
    if query.strip():
        search_where = "WHERE e.material_code LIKE ? OR e.material_name LIKE ? OR e.spec_model LIKE ?"
        params.extend([pattern, pattern, pattern])
    status_where = completion_filter_sql(status_filter)
    params.extend([limit, offset])
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            WITH material_rows AS (
                {internal_materials_base_sql(search_where)}
            )
            SELECT *
            FROM material_rows
            {status_where}
            ORDER BY row_no + 0, material_code
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def db_get_internal_files(material_code: str) -> list[dict[str, str]]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                id, created_at, document_type, original_filename, stored_path,
                COALESCE(file_note, '') AS file_note,
                mineru_status, mineru_batch_id, mineru_data_id, mineru_result_url,
                mineru_result_dir, mineru_result_json_path, mineru_error,
                mineru_model_version, mineru_updated_at
            FROM internal_material_files
            WHERE material_code = ?
            ORDER BY id DESC
            LIMIT 20
            """,
            (material_code,),
        ).fetchall()
    return [dict(row) for row in rows]


def db_upsert_internal_note(material_code: str, material_usage: str, process_name: str, note: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO internal_material_notes (
                material_code, material_usage, process_name, note, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(material_code) DO UPDATE SET
                material_usage = excluded.material_usage,
                process_name = excluded.process_name,
                note = excluded.note,
                updated_at = excluded.updated_at
            """,
            (
                material_code,
                material_usage,
                process_name,
                note,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()


def db_insert_internal_file(record: dict[str, str]) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO internal_material_files (
                created_at, material_code, document_type, original_filename, stored_path,
                uploader_ip, file_note, mineru_status, mineru_model_version, mineru_updated_at
            )
            VALUES (
                :created_at, :material_code, :document_type, :original_filename, :stored_path,
                :uploader_ip, :file_note, :mineru_status, :mineru_model_version, :mineru_updated_at
            )
            """,
            record,
        )
        conn.commit()
        return int(cursor.lastrowid)


def db_get_internal_file(file_id: int) -> dict[str, str]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT *
            FROM internal_material_files
            WHERE id = ?
            """,
            (file_id,),
        ).fetchone()
    if row is None:
        raise ValueError("没有找到内部资料文件记录")
    return dict(row)


def db_update_internal_file_mineru(file_id: int, updates: dict[str, str]) -> None:
    updates = {
        **updates,
        "mineru_updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    assignments = ", ".join(f"{key} = :{key}" for key in updates)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            f"UPDATE internal_material_files SET {assignments} WHERE id = :id",
            {**updates, "id": file_id},
        )
        conn.commit()


def db_update_internal_file_note(file_id: int, file_note: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE internal_material_files SET file_note = ? WHERE id = ?",
            (file_note, file_id),
        )
        conn.commit()


def mineru_json_request(method: str, url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Bearer {MINERU_API_KEY}",
        "Content-Type": "application/json",
    }
    request = urlrequest.Request(url, data=data, headers=headers, method=method)
    try:
        with urlrequest.urlopen(request, timeout=MINERU_REQUEST_TIMEOUT) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urlerror.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"MinerU HTTP {exc.code}: {detail[:500]}") from exc
    except urlerror.URLError as exc:
        raise RuntimeError(f"MinerU 请求失败：{exc.reason}") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"MinerU 返回了非 JSON 内容：{body[:500]}") from exc


def mineru_upload_to_presigned_url(upload_url: str, content: bytes) -> None:
    request = urlrequest.Request(upload_url, data=content, method="PUT")
    request.add_header("Content-Length", str(len(content)))
    try:
        with urlrequest.urlopen(request, timeout=MINERU_REQUEST_TIMEOUT) as response:
            if response.status >= 400:
                raise RuntimeError(f"MinerU 文件上传失败：HTTP {response.status}")
    except urlerror.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"MinerU 文件上传失败：HTTP {exc.code}: {detail[:500]}") from exc
    except urlerror.URLError as exc:
        raise RuntimeError(f"MinerU 文件上传失败：{exc.reason}") from exc


def mineru_result_dir(file_id: int) -> Path:
    return MINERU_RESULTS_DIR / str(file_id)


def write_mineru_response(file_id: int, payload: dict) -> Path:
    directory = mineru_result_dir(file_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_result.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def pick_mineru_extract_item(payload: dict, data_id: str = "") -> dict:
    data = payload.get("data") if isinstance(payload, dict) else {}
    if not isinstance(data, dict):
        return {}
    extract_result = data.get("extract_result")
    if isinstance(extract_result, list):
        if data_id:
            for item in extract_result:
                if isinstance(item, dict) and item.get("data_id") == data_id:
                    return item
        for item in extract_result:
            if isinstance(item, dict):
                return item
    if isinstance(extract_result, dict):
        return extract_result
    return data


def download_mineru_zip(file_id: int, zip_url: str) -> str:
    directory = mineru_result_dir(file_id)
    directory.mkdir(parents=True, exist_ok=True)
    zip_path = directory / "mineru_result.zip"
    request = urlrequest.Request(zip_url, method="GET")
    try:
        with urlrequest.urlopen(request, timeout=MINERU_REQUEST_TIMEOUT) as response:
            zip_path.write_bytes(response.read())
    except urlerror.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"MinerU 结果下载失败：HTTP {exc.code}: {detail[:500]}") from exc
    except urlerror.URLError as exc:
        raise RuntimeError(f"MinerU 结果下载失败：{exc.reason}") from exc

    try:
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(directory)
    except zipfile.BadZipFile:
        pass
    return str(directory)


def mineru_refresh_file(file_id: int) -> dict[str, str]:
    record = db_get_internal_file(file_id)
    batch_id = record.get("mineru_batch_id") or ""
    if not MINERU_API_KEY:
        updates = {
            "mineru_status": "not_configured",
            "mineru_error": "MINERU_API_KEY 未配置，文件已保存但未解析。",
        }
        db_update_internal_file_mineru(file_id, updates)
        return updates
    if not batch_id:
        return mineru_submit_file(file_id)

    result_url = MINERU_BATCH_RESULTS_API.format(batch_id=batch_id)
    payload = mineru_json_request("GET", result_url)
    response_path = write_mineru_response(file_id, payload)
    item = pick_mineru_extract_item(payload, record.get("mineru_data_id") or "")
    state = str(item.get("state") or item.get("status") or payload.get("msg") or "unknown")
    error_msg = str(item.get("err_msg") or item.get("error") or "")
    zip_url = str(item.get("full_zip_url") or item.get("zip_url") or "")
    result_dir = str(record.get("mineru_result_dir") or "")
    normalized_state = state.lower()
    if zip_url and normalized_state in {"done", "success", "completed", "complete"}:
        result_dir = download_mineru_zip(file_id, zip_url)
    updates = {
        "mineru_status": state,
        "mineru_result_url": zip_url,
        "mineru_result_dir": result_dir,
        "mineru_result_json_path": str(response_path),
        "mineru_error": error_msg,
    }
    db_update_internal_file_mineru(file_id, updates)
    return updates


def mineru_submit_file(file_id: int) -> dict[str, str]:
    record = db_get_internal_file(file_id)
    if not MINERU_API_KEY:
        updates = {
            "mineru_status": "not_configured",
            "mineru_error": "MINERU_API_KEY 未配置，文件已保存但未解析。",
        }
        db_update_internal_file_mineru(file_id, updates)
        return updates

    file_path = Path(record["stored_path"])
    if not file_path.exists():
        raise ValueError("内部资料文件不存在，无法提交 MinerU 解析")

    data_id = record.get("mineru_data_id") or f"internal-{file_id}-{uuid.uuid4().hex[:8]}"
    payload = {
        "model_version": MINERU_MODEL_VERSION,
        "enable_formula": MINERU_ENABLE_FORMULA,
        "enable_table": MINERU_ENABLE_TABLE,
        "language": MINERU_LANGUAGE,
        "files": [
            {
                "name": record["original_filename"],
                "is_ocr": MINERU_ENABLE_OCR,
                "data_id": data_id,
            }
        ],
    }
    response = mineru_json_request("POST", MINERU_FILE_URLS_API, payload)
    response_path = write_mineru_response(file_id, response)
    data = response.get("data") if isinstance(response, dict) else {}
    if not isinstance(data, dict):
        raise RuntimeError(f"MinerU 上传 URL 返回格式异常：{response}")
    batch_id = str(data.get("batch_id") or "")
    file_urls = data.get("file_urls") or data.get("file_urls_list") or []
    if not batch_id or not file_urls:
        raise RuntimeError(f"MinerU 没有返回 batch_id/file_urls：{response}")

    upload_target = file_urls[0]
    if isinstance(upload_target, dict):
        upload_target = upload_target.get("url") or upload_target.get("upload_url") or ""
    if not upload_target:
        raise RuntimeError(f"MinerU 上传 URL 为空：{response}")
    content = file_path.read_bytes()
    mineru_upload_to_presigned_url(str(upload_target), content)
    updates = {
        "mineru_status": "submitted",
        "mineru_batch_id": batch_id,
        "mineru_data_id": data_id,
        "mineru_result_json_path": str(response_path),
        "mineru_error": "",
        "mineru_model_version": MINERU_MODEL_VERSION,
    }
    db_update_internal_file_mineru(file_id, updates)

    deadline = time.time() + MINERU_MAX_WAIT_SECONDS
    latest = updates
    while time.time() < deadline:
        time.sleep(MINERU_POLL_SECONDS)
        latest = mineru_refresh_file(file_id)
        status = (latest.get("mineru_status") or "").lower()
        if status in {"done", "success", "completed", "complete", "failed", "error"}:
            break
    return latest


def select_options(options: list[str], selected: str = "") -> str:
    parts = []
    for option in options:
        attr = " selected" if option == selected else ""
        parts.append(f'<option value="{html.escape(option)}"{attr}>{html.escape(option)}</option>')
    return "\n".join(parts)


def render_success_notice(message: str, details: dict[str, str] | None = None) -> str:
    if not message:
        return ""
    if not details:
        return f'<div class="notice success">{html.escape(message)}</div>'

    rows = []
    for label, value in details.items():
        display_value = value if value else "未填写"
        rows.append(
            "<div class=\"success-row\">"
            f"<dt>{html.escape(label)}</dt>"
            f"<dd>{html.escape(display_value)}</dd>"
            "</div>"
        )
    return (
        '<div class="notice success">'
        f'<div class="success-title">{html.escape(message)}</div>'
        f'<dl class="success-details">{"".join(rows)}</dl>'
        "</div>"
    )


def safe_next_url(value: str) -> str:
    if not value:
        return app_url("/")
    if BASE_PATH and value.startswith(BASE_PATH + "/"):
        return value
    if not BASE_PATH and value.startswith("/") and not value.startswith("//"):
        return value
    return app_url("/")


def render_login_page(error: str = "", next_url: str = "") -> bytes:
    error_html = f'<div class="notice error">{html.escape(error)}</div>' if error else ""
    next_value = html.escape(safe_next_url(next_url), quote=True)
    body = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>物料文件上传系统</title>
  <link rel="stylesheet" href="{app_url("/static/styles.css")}">
</head>
<body class="login-body">
  <main class="login-main">
    <section class="login-panel">
      <h1>物料文件上传系统</h1>
      <p>请输入密码后继续上传资料。</p>
      {error_html}
      <form action="{app_url("/login")}" method="post" class="login-form">
        <input type="hidden" name="next" value="{next_value}">
        <label>
          访问密码
          <input type="password" name="password" autocomplete="current-password" autofocus required>
        </label>
        <button type="submit">进入系统</button>
      </form>
    </section>
  </main>
</body>
</html>"""
    return body.encode("utf-8")


def render_page(
    message: str = "",
    error: str = "",
    values: dict[str, str] | None = None,
    success_details: dict[str, str] | None = None,
) -> bytes:
    values = values or {}
    def field_value(key: str) -> str:
        return html.escape(values.get(key, ""), quote=True)

    message_html = render_success_notice(message, success_details)
    error_html = f'<div class="notice error">{html.escape(error)}</div>' if error else ""

    body = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>依耐克物料资料上传</title>
  <link rel="stylesheet" href="{app_url("/static/styles.css")}">
</head>
<body>
  <main>
    <section class="hero">
      <div>
        <h1>依耐克物料资料上传</h1>
        <p>请上传 TDS、SDS/MSDS、检测报告、图片、表格或其他资料文件。一个文件提交一次；同一物料可重复提交多个资料。</p>
      </div>
      <div class="status">
        <span>Any file</span>
        <strong>最大 {MAX_UPLOAD_MB} MB</strong>
      </div>
    </section>

    {message_html}
    {error_html}

    <section class="panel">
      <form action="{app_url("/upload")}" method="post" enctype="multipart/form-data">
        <div class="grid">
          <label>
            物料型号/牌号
            <input name="material_code" value="{field_value("material_code")}" placeholder="例如 EC-GM62-C20-57564" required>
          </label>
          <label>
            供应商/品牌
            <input name="supplier" value="{field_value("supplier")}" placeholder="供应商名称" required>
          </label>
          <label>
            物料功能/作用
            <select name="material_function" required>
              <option value="">请选择</option>
              {select_options(FUNCTION_TYPES, values.get("material_function", ""))}
            </select>
            <input class="other-input" data-other-for="material_function" name="material_function_other" value="{field_value("material_function_other")}" placeholder="选择其他时，请填写具体功能/作用">
          </label>
          <label>
            资料类型
            <select name="document_type" required>
              <option value="">请选择</option>
              {select_options(DOCUMENT_TYPES)}
            </select>
            <input class="other-input" data-other-for="document_type" name="document_type_other" placeholder="选择其他时，请填写具体资料类型">
          </label>
          <label>
            适用基材
            <input name="substrate" value="{field_value("substrate")}" placeholder="例如 SUS、不锈钢、铝合金" required>
          </label>
          <label>
            适用工艺/工序
            <select name="process_name" required>
              <option value="">请选择</option>
              {select_options(PROCESS_TYPES, values.get("process_name", ""))}
            </select>
            <input class="other-input" data-other-for="process_name" name="process_name_other" value="{field_value("process_name_other")}" placeholder="选择其他时，请填写具体工艺/工序">
          </label>
        </div>

        <label>
          资料文件
          <input type="file" name="file" required>
        </label>

        <label>
          备注
          <textarea name="note" rows="3" placeholder="例如：该资料无推荐固化条件；配比需现场试验确认">{html.escape(values.get("note", ""))}</textarea>
        </label>

        <button type="submit">上传资料</button>
      </form>
    </section>

  </main>
  <script>
    function syncOtherInput(select) {{
      const input = document.querySelector('[data-other-for="' + select.name + '"]');
      if (!input) return;
      const active = select.value === '其他';
      input.classList.toggle('is-visible', active);
      input.required = active;
      if (!active) input.value = '';
    }}
    document.querySelectorAll('select').forEach(function(select) {{
      select.addEventListener('change', function() {{ syncOtherInput(select); }});
      syncOtherInput(select);
    }});
  </script>
</body>
</html>"""
    return body.encode("utf-8")


def render_internal_page(
    message: str = "",
    error: str = "",
    query: str = "",
    expanded_code: str = "",
    status_filter: str = "all",
    page: int = 1,
    per_page: int = 20,
    saved_code: str = "",
) -> bytes:
    status_filter = normalize_status_filter(status_filter)
    per_page = normalize_per_page(per_page)
    page = normalize_page(page)
    if saved_code and not message:
        message = f"已保存：{saved_code}"
    message_html = render_success_notice(message)
    error_html = f'<div class="notice error">{html.escape(error)}</div>' if error else ""
    conflict = db_get_latest_erp_conflict()
    conflict_html = ""
    if conflict:
        conflict_lines = [
            line.strip()
            for line in (conflict.get("conflict_text") or "").splitlines()
            if line.strip()
        ]
        conflict_items = "".join(
            f"<li>{html.escape(line)}</li>"
            for line in conflict_lines[:80]
        )
        remaining = len(conflict_lines) - 80
        if remaining > 0:
            conflict_items += f"<li>另有 {remaining} 条未显示</li>"
        conflict_html = (
            '<section class="conflict-report">'
            '<div class="conflict-report-head">'
            '<div>'
            "<h2>ERP 导入冲突报告</h2>"
            f'<p>{html.escape(conflict["created_at"])} | {html.escape(conflict["source_filename"])} | 共 {int(conflict["conflict_count"])} 条</p>'
            "</div>"
            f'<form action="{app_url("/internal/materials/clear-conflicts")}" method="post">'
            '<button type="submit">清除提示</button>'
            "</form>"
            "</div>"
            "<p>导入已取消。请按下面的物料编号搜索，人工核对并修改 ERP 清单或既有记录后再导入。</p>"
            f"<ol>{conflict_items}</ol>"
            "</section>"
        )
    material_count = db_count_erp_materials()
    filtered_count = db_count_internal_materials(query, status_filter)
    total_pages = max((filtered_count + per_page - 1) // per_page, 1)
    page = min(page, total_pages)
    offset = (page - 1) * per_page
    materials = db_get_internal_materials(query, status_filter, per_page, offset)
    state_hidden = (
        f'<input type="hidden" name="q" value="{html.escape(query, quote=True)}">'
        f'<input type="hidden" name="status" value="{html.escape(status_filter, quote=True)}">'
        f'<input type="hidden" name="page" value="{page}">'
        f'<input type="hidden" name="per_page" value="{per_page}">'
    )

    if not materials:
        records_html = (
            '<div class="empty-cell">'
            "暂无物料记录。请先上传 ERP 导出的物料编码列表。"
            "</div>"
        )
    else:
        records: list[str] = []
        for item in materials:
            code = item["material_code"]
            file_count = item["file_count"]
            note_updated = item["note_updated_at"] or "未补录"
            files_html = ""
            if file_count:
                file_rows = []
                for file_item in db_get_internal_files(code):
                    download_url = app_url(
                        "/internal/materials/download-file?"
                        + urlencode({"file_id": str(file_item["id"])})
                    )
                    file_rows.append(
                        "<li>"
                        '<div class="file-record-head">'
                        f"<span>{html.escape(file_item['created_at'])}</span>"
                        f"<span>{html.escape(file_item['document_type'])}</span>"
                        f'<a href="{html.escape(download_url, quote=True)}">{html.escape(file_item["original_filename"])}</a>'
                        "</div>"
                        f'<form action="{app_url("/internal/materials/save-file-note")}" method="post" class="file-note-form">'
                        f'<input type="hidden" name="file_id" value="{html.escape(str(file_item["id"]), quote=True)}">'
                        f'<input type="hidden" name="material_code" value="{html.escape(code, quote=True)}">'
                        f"{state_hidden}"
                        f'<textarea name="file_note" rows="2" placeholder="这份文档的备注">{html.escape(file_item.get("file_note") or "")}</textarea>'
                        '<button type="submit">保存文档备注</button>'
                        "</form>"
                        "</li>"
                    )
                files_html = (
                    '<div class="internal-files">'
                    "<strong>已上传文档</strong>"
                    f"<ul>{''.join(file_rows) if file_rows else '<li>暂无文件</li>'}</ul>"
                    "</div>"
                )

            meta_items = [
                ("序号", item["row_no"]),
                ("规格型号", item["spec_model"]),
                ("单位", item["unit"]),
                ("类别", item["category"]),
                ("供应商编号", item["supplier_code"]),
                ("最低警戒数", item["min_alert"]),
                ("最高警戒数", item["max_alert"]),
            ]
            meta_html = "".join(
                '<div class="meta-item">'
                f"<span>{html.escape(label)}</span>"
                f"<strong>{html.escape(value or '空')}</strong>"
                "</div>"
                for label, value in meta_items
            )
            has_usage = bool((item["material_usage"] or "").strip())
            has_process = bool((item["process_name"] or "").strip())
            has_document = int(file_count) > 0
            missing_parts = []
            if not has_usage:
                missing_parts.append("用途")
            if not has_process:
                missing_parts.append("工序")
            if not has_document:
                missing_parts.append("文档")
            status_class = "is-complete" if not missing_parts else "is-incomplete"
            status_text = "完成" if not missing_parts else "未完成"
            missing_text = "都已填写" if not missing_parts else "缺：" + "、".join(missing_parts)
            latest_import_text = (
                "最新清单"
                if int(item.get("active_in_latest_import") or 0)
                else "旧清单保留"
            )
            records.append(
                '<details class="material-record">'
                '<summary class="material-record-head">'
                '<div class="material-title">'
                f'<strong class="material-code">{html.escape(code)}</strong>'
                f'<span class="material-name">{html.escape(item["material_name"])}</span>'
                "</div>"
                '<div class="record-summary-status">'
                f'<span class="status-badge {status_class}">{html.escape(status_text)}</span>'
                f'<span class="summary-chip">{html.escape(missing_text)}</span>'
                f'<span class="summary-chip">文件 {int(file_count)}</span>'
                f'<span class="summary-chip">{html.escape(latest_import_text)}</span>'
                '<span class="toggle-label"><span class="when-open">收起条目</span><span class="when-closed">展开填写</span></span>'
                "</div>"
                "</summary>"
                f'<div class="material-meta">{meta_html}</div>'
                '<div class="material-actions">'
                f'<form action="{app_url("/internal/materials/upload-file")}" method="post" enctype="multipart/form-data" class="combined-record-form">'
                f'<input type="hidden" name="material_code" value="{html.escape(code, quote=True)}">'
                f"{state_hidden}"
                '<section class="action-panel">'
                "<h3>用途/工序补录</h3>"
                f'<input name="material_usage" value="{html.escape(item["material_usage"], quote=True)}" placeholder="用途/作用" required>'
                f'<select name="process_name" required><option value="">工序-下拉选择</option>{select_options(PROCESS_TYPES, item["process_name"])}</select>'
                f'<textarea name="note" rows="2" placeholder="内部备注">{html.escape(item["note"])}</textarea>'
                f'<span class="muted">更新：{html.escape(note_updated)}</span>'
                "</section>"
                '<section class="action-panel">'
                "<h3>上传文档</h3>"
                f'<select name="document_type" required>{select_options(DOCUMENT_TYPES)}</select>'
                '<input class="other-input" data-other-for="document_type" name="document_type_other" placeholder="选择其他时，请填写具体资料类型">'
                '<input type="file" name="file" required>'
                '<textarea name="file_note" rows="2" placeholder="这份文档的备注，可先不填"></textarea>'
                "</section>"
                '<div class="combined-submit">'
                '<button type="submit">保存并上传</button>'
                '<span class="muted">填写用途、工序并选择文件后提交；保存后补录内容会保留显示。</span>'
                "</div>"
                "</form>"
                f"{files_html}"
                "</div>"
                "</details>"
            )
        records_html = "".join(records)

    status_options_html = "".join(
        f'<option value="{html.escape(value, quote=True)}"{" selected" if value == status_filter else ""}>{html.escape(label)}</option>'
        for value, label in STATUS_FILTERS.items()
    )
    per_page_options_html = "".join(
        f'<option value="{value}"{" selected" if value == per_page else ""}>{value} 个/页</option>'
        for value in PAGE_SIZE_OPTIONS
    )
    page_start = 0 if filtered_count == 0 else offset + 1
    page_end = min(offset + per_page, filtered_count)
    prev_page = max(page - 1, 1)
    next_page = min(page + 1, total_pages)
    prev_url = internal_materials_url(query, "", status_filter, prev_page, per_page)
    next_url = internal_materials_url(query, "", status_filter, next_page, per_page)
    pagination_html = (
        '<div class="pagination">'
        f'<span>显示 {page_start}-{page_end} / {filtered_count} 条</span>'
        f'<a class="page-link{" is-disabled" if page <= 1 else ""}" href="{html.escape(prev_url, quote=True)}">上一页</a>'
        f'<span>第 {page} / {total_pages} 页</span>'
        f'<a class="page-link{" is-disabled" if page >= total_pages else ""}" href="{html.escape(next_url, quote=True)}">下一页</a>'
        "</div>"
    )

    body = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>依耐克内部物料维护</title>
  <link rel="stylesheet" href="{app_url("/static/styles.css")}">
</head>
<body>
  <main>
    <section class="hero">
      <div>
        <h1>依耐克内部物料维护</h1>
        <p>同步 ERP 物料编码列表后，内部人员逐条补充用途、适用工序，并给物料挂载资料文件。</p>
      </div>
      <div class="status">
        <span>ERP materials</span>
        <strong>{material_count} 条</strong>
      </div>
    </section>

    {message_html}
    {error_html}
    {conflict_html}

    <section class="panel">
      <div class="section-title">
        <h2>ERP 清单导入</h2>
        <span>同步会新增/更新 ERP 基础字段，保留已补录的用途、工序和文件；旧物料不会删除</span>
      </div>
      <form action="{app_url("/internal/materials/import-erp")}" method="post" enctype="multipart/form-data" class="internal-import">
        <label>
          ERP 物料编码列表（xlsx）
          <input type="file" name="erp_file" accept=".xlsx" required>
        </label>
        <button type="submit">同步 ERP 清单</button>
      </form>
    </section>

    <section class="panel">
      <div class="section-title">
        <h2>物料补录</h2>
        <span>可按完成状态筛选，并分页查看</span>
      </div>
      <form action="{app_url("/internal/materials/")}" method="get" class="list-controls">
        <label>
          搜索
          <input name="q" value="{html.escape(query, quote=True)}" placeholder="物料编号、名称或规格">
        </label>
        <label>
          状态
          <select name="status">{status_options_html}</select>
        </label>
        <label>
          每页
          <select name="per_page">{per_page_options_html}</select>
        </label>
        <button type="submit">筛选</button>
      </form>
      {pagination_html}
      <div class="material-list">{records_html}</div>
      {pagination_html}
    </section>
  </main>
  <script>
    function syncOtherInput(select) {{
      const form = select.closest('form') || document;
      const input = form.querySelector('[data-other-for="' + select.name + '"]');
      if (!input) return;
      const active = select.value === '其他';
      input.classList.toggle('is-visible', active);
      input.required = active;
      if (!active) input.value = '';
    }}
    document.querySelectorAll('select').forEach(function(select) {{
      select.addEventListener('change', function() {{ syncOtherInput(select); }});
      syncOtherInput(select);
    }});
    const listControls = document.querySelector('.list-controls');
    if (listControls) {{
      listControls.querySelectorAll('select').forEach(function(select) {{
        select.addEventListener('change', function() {{
          if (listControls.requestSubmit) {{
            listControls.requestSubmit();
          }} else {{
            listControls.submit();
          }}
        }});
      }});
    }}
  </script>
</body>
</html>"""
    return body.encode("utf-8")


class UploadHandler(BaseHTTPRequestHandler):
    def is_authenticated(self, scope: str) -> bool:
        cookies = parse_cookies(self.headers.get("Cookie"))
        return is_valid_auth_token(scope, cookies.get(AUTH_COOKIE_NAMES[scope], ""))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = route_path(parsed.path)
        if path is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        if path == "/static/styles.css":
            self.send_static_css()
            return
        if path == "/":
            if not self.is_authenticated(UPLOAD_AUTH_SCOPE):
                self.send_html(render_login_page(next_url=self.path))
                return
            self.send_html(render_page())
            return
        if path == "/internal/materials/":
            if not self.is_authenticated(INTERNAL_AUTH_SCOPE):
                self.send_html(render_login_page(next_url=self.path))
                return
            params = parse_qs(parsed.query)
            query = params.get("q", [""])[0]
            expanded_code = params.get("show_files", [""])[0]
            status_filter = params.get("status", ["all"])[0]
            page = normalize_page(params.get("page", ["1"])[0])
            per_page = normalize_per_page(params.get("per_page", ["20"])[0])
            saved_code = params.get("saved", [""])[0]
            self.send_html(
                render_internal_page(
                    query=query,
                    expanded_code=expanded_code,
                    status_filter=status_filter,
                    page=page,
                    per_page=per_page,
                    saved_code=saved_code,
                )
            )
            return
        if path == "/internal/materials/refresh-mineru":
            if not self.is_authenticated(INTERNAL_AUTH_SCOPE):
                self.send_html(render_login_page(next_url=self.path))
                return
            try:
                self.handle_internal_mineru_refresh(parsed.query)
            except Exception as exc:
                self.send_html(render_internal_page(error=str(exc)), status=HTTPStatus.BAD_REQUEST)
            return
        if path == "/internal/materials/download-file":
            if not self.is_authenticated(INTERNAL_AUTH_SCOPE):
                self.send_html(render_login_page(next_url=self.path))
                return
            try:
                self.handle_internal_file_download(parsed.query)
            except Exception as exc:
                self.send_html(render_internal_page(error=str(exc)), status=HTTPStatus.NOT_FOUND)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = route_path(parsed.path)
        if path == "/login":
            self.handle_login()
            return
        if path != "/upload":
            if path in {
                "/internal/materials/import-erp",
                "/internal/materials/save",
                "/internal/materials/upload-file",
                "/internal/materials/save-file-note",
                "/internal/materials/clear-conflicts",
            }:
                if not self.is_authenticated(INTERNAL_AUTH_SCOPE):
                    self.send_html(render_login_page("请先输入密码。", next_url=self.path), status=HTTPStatus.UNAUTHORIZED)
                    return
                try:
                    if path == "/internal/materials/import-erp":
                        self.handle_internal_erp_import()
                    elif path == "/internal/materials/save":
                        self.handle_internal_note_save()
                    elif path == "/internal/materials/save-file-note":
                        self.handle_internal_file_note_save()
                    elif path == "/internal/materials/clear-conflicts":
                        self.handle_internal_clear_conflicts()
                    else:
                        self.handle_internal_file_upload()
                except Exception as exc:
                    self.send_html(
                        render_internal_page(error=str(exc)),
                        status=HTTPStatus.BAD_REQUEST,
                    )
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        if not self.is_authenticated(UPLOAD_AUTH_SCOPE):
            self.send_html(render_login_page("请先输入密码。", next_url=self.path), status=HTTPStatus.UNAUTHORIZED)
            return
        try:
            self.handle_upload()
        except Exception as exc:
            self.send_html(render_page(error=str(exc)), status=HTTPStatus.BAD_REQUEST)

    def handle_login(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0 or content_length > 4096:
            self.send_html(render_login_page("请输入密码。"), status=HTTPStatus.BAD_REQUEST)
            return

        body = self.rfile.read(content_length).decode("utf-8", errors="replace")
        fields = parse_qs(body, keep_blank_values=True)
        password = fields.get("password", [""])[0]
        next_url = safe_next_url(fields.get("next", [""])[0])
        scope = auth_scope_for_path(route_path(urlparse(next_url).path))
        if not hmac.compare_digest(password, password_for_scope(scope)):
            self.send_html(render_login_page("密码错误，请重试。", next_url=next_url), status=HTTPStatus.UNAUTHORIZED)
            return

        self.send_redirect(next_url, headers={"Set-Cookie": auth_cookie_header(scope)})

    def read_request_body(self, max_mb: int | None = None) -> bytes:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            raise ValueError("没有收到提交内容")
        limit_mb = max_mb or MAX_UPLOAD_MB
        if content_length > limit_mb * 1024 * 1024:
            raise ValueError(f"提交内容超过限制：最大 {limit_mb} MB")
        return self.rfile.read(content_length)

    def parse_urlencoded_fields(self) -> dict[str, str]:
        body = self.read_request_body(max_mb=1).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        return {key: values[0].strip() if values else "" for key, values in parsed.items()}

    def parse_multipart_request(self) -> tuple[dict[str, str], dict[str, dict[str, bytes | str]]]:
        body = self.read_request_body()
        return parse_multipart(self.headers.get("Content-Type", ""), body)

    def handle_internal_erp_import(self) -> None:
        fields, files = self.parse_multipart_request()
        file_item = files.get("erp_file")
        if file_item is None or not file_item.get("filename"):
            raise ValueError("请选择 ERP 物料编码列表 xlsx 文件")

        original_filename = Path(str(file_item["filename"])).name
        if not original_filename.lower().endswith(".xlsx"):
            raise ValueError("当前只支持上传 .xlsx 文件")
        content = file_item["content"]
        if not isinstance(content, bytes) or not content:
            raise ValueError("ERP 文件内容为空")

        rows = read_xlsx_rows(content)
        materials = normalize_erp_rows(rows)
        try:
            stats = db_sync_erp_materials(materials, original_filename)
        except ErpConflictError as exc:
            db_save_erp_conflicts(original_filename, exc.conflicts)
            self.send_html(render_internal_page(error=str(exc)), status=HTTPStatus.BAD_REQUEST)
            return
        self.send_html(
            render_internal_page(
                message=(
                    "ERP 清单同步完成："
                    f"本次清单 {stats['total']} 条，"
                    f"新增 {stats['created']} 条，"
                    f"更新 {stats['updated']} 条，"
                    f"旧清单保留 {stats['inactive']} 条。"
                )
            )
        )

    def handle_internal_clear_conflicts(self) -> None:
        db_clear_erp_conflicts()
        self.send_redirect(internal_materials_url())

    def handle_internal_note_save(self) -> None:
        fields = self.parse_urlencoded_fields()
        material_code = require_text(fields, "material_code", "物料编号")
        material_usage = require_text(fields, "material_usage", "用途/作用")
        process_name = require_text(fields, "process_name", "适用工序")
        note = get_text(fields, "note")
        query = get_text(fields, "q")
        status_filter = normalize_status_filter(get_text(fields, "status"))
        page = normalize_page(get_text(fields, "page"))
        per_page = normalize_per_page(get_text(fields, "per_page"))
        db_upsert_internal_note(material_code, material_usage, process_name, note)
        self.send_redirect(internal_materials_url(query, "", status_filter, page, per_page, material_code))

    def handle_internal_file_upload(self) -> None:
        fields, files = self.parse_multipart_request()
        material_code = require_text(fields, "material_code", "物料编号")
        material_usage = require_text(fields, "material_usage", "用途/作用")
        process_name = require_text(fields, "process_name", "适用工序")
        note = get_text(fields, "note")
        document_type = resolve_other_choice(fields, "document_type", "资料类型")
        file_note = get_text(fields, "file_note")
        query = get_text(fields, "q")
        status_filter = normalize_status_filter(get_text(fields, "status"))
        page = normalize_page(get_text(fields, "page"))
        per_page = normalize_per_page(get_text(fields, "per_page"))

        file_item = files.get("file")
        if file_item is None or not file_item.get("filename"):
            raise ValueError("请选择资料文件")
        original_filename = Path(str(file_item["filename"])).name
        content = file_item["content"]
        if not isinstance(content, bytes) or not content:
            raise ValueError("文件内容为空，请确认后重新上传")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique = uuid.uuid4().hex[:8]
        safe_filename = f"{timestamp}_{unique}_{safe_segment(original_filename, 'document')}"
        material_dir = INTERNAL_FILES_DIR / safe_segment(material_code) / safe_segment(document_type)
        material_dir.mkdir(parents=True, exist_ok=True)
        local_path = material_dir / safe_filename
        local_path.write_bytes(content)

        db_upsert_internal_note(material_code, material_usage, process_name, note)
        file_id = db_insert_internal_file(
            {
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "material_code": material_code,
                "document_type": document_type,
                "original_filename": original_filename,
                "stored_path": str(local_path),
                "uploader_ip": self.client_address[0],
                "file_note": file_note,
                "mineru_status": "saved",
                "mineru_model_version": MINERU_MODEL_VERSION,
                "mineru_updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        if MINERU_AUTO_PARSE:
            try:
                mineru_submit_file(file_id)
            except Exception as exc:
                db_update_internal_file_mineru(
                    file_id,
                    {
                        "mineru_status": "submit_failed",
                        "mineru_error": str(exc),
                        "mineru_model_version": MINERU_MODEL_VERSION,
                    },
                )
        self.send_redirect(internal_materials_url(query, "", status_filter, page, per_page, material_code))

    def handle_internal_file_note_save(self) -> None:
        fields = self.parse_urlencoded_fields()
        file_id_text = require_text(fields, "file_id", "文件 ID")
        if not file_id_text.isdigit():
            raise ValueError("文件 ID 无效")
        material_code = require_text(fields, "material_code", "物料编号")
        query = get_text(fields, "q")
        status_filter = normalize_status_filter(get_text(fields, "status"))
        page = normalize_page(get_text(fields, "page"))
        per_page = normalize_per_page(get_text(fields, "per_page"))
        file_note = get_text(fields, "file_note")
        db_update_internal_file_note(int(file_id_text), file_note)
        self.send_redirect(internal_materials_url(query, "", status_filter, page, per_page, material_code))

    def handle_internal_file_download(self, query_string: str) -> None:
        params = parse_qs(query_string)
        file_id_text = params.get("file_id", [""])[0]
        if not file_id_text.isdigit():
            raise ValueError("缺少有效的文件 ID")
        record = db_get_internal_file(int(file_id_text))
        file_path = Path(record["stored_path"]).resolve()
        internal_root = INTERNAL_FILES_DIR.resolve()
        try:
            file_path.relative_to(internal_root)
        except ValueError as exc:
            raise ValueError("文件路径无效") from exc
        if not file_path.exists() or not file_path.is_file():
            raise ValueError("文件不存在")
        data = file_path.read_bytes()
        filename = record.get("original_filename") or file_path.name
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename)}")
        self.end_headers()
        self.wfile.write(data)

    def handle_internal_mineru_refresh(self, query_string: str) -> None:
        params = parse_qs(query_string)
        file_id_text = params.get("file_id", [""])[0]
        if not file_id_text.isdigit():
            raise ValueError("缺少有效的文件 ID")
        query = params.get("q", [""])[0]
        expanded_code = params.get("show_files", [""])[0]
        status_filter = normalize_status_filter(params.get("status", ["all"])[0])
        page = normalize_page(params.get("page", ["1"])[0])
        per_page = normalize_per_page(params.get("per_page", ["20"])[0])
        mineru_refresh_file(int(file_id_text))
        self.send_redirect(internal_materials_url(query, expanded_code, status_filter, page, per_page))

    def handle_upload(self) -> None:
        fields, files = self.parse_multipart_request()

        material_code = require_text(fields, "material_code", "物料型号/牌号")
        supplier = require_text(fields, "supplier", "供应商/品牌")
        material_function = resolve_other_choice(fields, "material_function", "物料功能/作用")
        substrate = require_text(fields, "substrate", "适用基材")
        process_name = resolve_other_choice(fields, "process_name", "适用工艺/工序")
        document_type = resolve_other_choice(fields, "document_type", "资料类型")
        note = get_text(fields, "note")

        file_item = files.get("file")
        if file_item is None or not file_item.get("filename"):
            raise ValueError("请选择资料文件")

        original_filename = Path(str(file_item["filename"])).name
        content = file_item["content"]
        if not isinstance(content, bytes):
            raise ValueError("文件读取失败")
        if not content:
            raise ValueError("文件内容为空，请确认后重新上传")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique = uuid.uuid4().hex[:8]
        safe_filename = f"{timestamp}_{unique}_{safe_segment(original_filename, 'document')}"
        material_dir = FILES_DIR / safe_segment(material_code) / safe_segment(document_type)
        material_dir.mkdir(parents=True, exist_ok=True)
        local_path = material_dir / safe_filename
        local_path.write_bytes(content)

        upload_id = db_insert(
            {
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "material_code": material_code,
                "supplier": supplier,
                "material_function": material_function,
                "substrate": substrate,
                "process_name": process_name,
                "document_type": document_type,
                "original_filename": original_filename,
                "stored_path": str(local_path),
                "storage_backend": "local",
                "note": note,
                "uploader_ip": self.client_address[0],
            }
        )
        sticky_values = {
            key: value
            for key, value in fields.items()
            if key not in {"document_type", "document_type_other"}
        }
        success_details = {
            "记录 ID": str(upload_id),
            "物料型号/牌号": material_code,
            "供应商/品牌": supplier,
            "物料功能/作用": material_function,
            "适用基材": substrate,
            "适用工艺/工序": process_name,
            "资料类型": document_type,
            "上传文件": original_filename,
            "备注": note,
        }
        self.send_html(
            render_page(
                message="上传成功",
                values=sticky_values,
                success_details=success_details,
            )
        )

    def send_static_css(self) -> None:
        path = BASE_DIR / "static" / "styles.css"
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/css; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_html(self, data: bytes, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_redirect(self, location: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), fmt % args))


def main() -> None:
    init_storage()
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), UploadHandler)
    print(f"Yinaike material upload portal: http://{host}:{port}{BASE_PATH or '/'}")
    server.serve_forever()


if __name__ == "__main__":
    main()
