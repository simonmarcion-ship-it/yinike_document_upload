from __future__ import annotations

import html
import hashlib
import hmac
import os
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("UPLOAD_DATA_DIR", BASE_DIR / "data")).resolve()
FILES_DIR = DATA_DIR / "files"
DB_PATH = DATA_DIR / "uploads.db"
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "30"))
RAW_BASE_PATH = os.environ.get("BASE_PATH", "").strip()
BASE_PATH = "" if RAW_BASE_PATH in {"", "/"} else "/" + RAW_BASE_PATH.strip("/")
UPLOAD_PASSWORD = os.environ.get("UPLOAD_PASSWORD", "20250605")
AUTH_SECRET = os.environ.get("AUTH_SECRET", f"yinike-material-upload:{UPLOAD_PASSWORD}")
AUTH_COOKIE_NAME = "yinike_upload_auth"
AUTH_MAX_AGE_SECONDS = int(os.environ.get("AUTH_MAX_AGE_SECONDS", str(7 * 24 * 60 * 60)))

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


def app_url(path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    return f"{BASE_PATH}{path}" if BASE_PATH else path


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


def sign_auth_expires(expires_at: str) -> str:
    return hmac.new(
        AUTH_SECRET.encode("utf-8"),
        expires_at.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def make_auth_token() -> str:
    expires_at = str(int(time.time()) + AUTH_MAX_AGE_SECONDS)
    return f"{expires_at}.{sign_auth_expires(expires_at)}"


def is_valid_auth_token(token: str) -> bool:
    try:
        expires_at, signature = token.split(".", 1)
        expires_int = int(expires_at)
    except ValueError:
        return False
    if expires_int < int(time.time()):
        return False
    return hmac.compare_digest(signature, sign_auth_expires(expires_at))


def auth_cookie_header() -> str:
    return (
        f"{AUTH_COOKIE_NAME}={make_auth_token()}; "
        f"Max-Age={AUTH_MAX_AGE_SECONDS}; Path={cookie_path()}; HttpOnly; SameSite=Lax"
    )


def init_storage() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FILES_DIR.mkdir(parents=True, exist_ok=True)
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


def render_login_page(error: str = "") -> bytes:
    error_html = f'<div class="notice error">{html.escape(error)}</div>' if error else ""
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


class UploadHandler(BaseHTTPRequestHandler):
    def is_authenticated(self) -> bool:
        cookies = parse_cookies(self.headers.get("Cookie"))
        return is_valid_auth_token(cookies.get(AUTH_COOKIE_NAME, ""))

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
            if not self.is_authenticated():
                self.send_html(render_login_page())
                return
            self.send_html(render_page())
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = route_path(parsed.path)
        if path == "/login":
            self.handle_login()
            return
        if path != "/upload":
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        if not self.is_authenticated():
            self.send_html(render_login_page("请先输入密码。"), status=HTTPStatus.UNAUTHORIZED)
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
        if not hmac.compare_digest(password, UPLOAD_PASSWORD):
            self.send_html(render_login_page("密码错误，请重试。"), status=HTTPStatus.UNAUTHORIZED)
            return

        self.send_redirect(app_url("/"), headers={"Set-Cookie": auth_cookie_header()})

    def handle_upload(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            raise ValueError("没有收到上传内容")
        if content_length > MAX_UPLOAD_MB * 1024 * 1024:
            raise ValueError(f"文件超过限制：最大 {MAX_UPLOAD_MB} MB")

        body = self.rfile.read(content_length)
        fields, files = parse_multipart(self.headers.get("Content-Type", ""), body)

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
