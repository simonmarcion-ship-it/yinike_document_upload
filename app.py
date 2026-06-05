from __future__ import annotations

import html
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
from urllib.parse import parse_qs, quote, urlparse


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("UPLOAD_DATA_DIR", BASE_DIR / "data")).resolve()
FILES_DIR = DATA_DIR / "files"
DB_PATH = DATA_DIR / "uploads.db"
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "30"))
UPLOAD_TOKEN = os.environ.get("UPLOAD_TOKEN", "").strip()
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()

DOCUMENT_TYPES = ["TDS", "SDS", "MSDS", "COA/检测报告", "其他"]
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


def db_recent(limit: int = 20) -> list[sqlite3.Row]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return list(
            conn.execute(
                """
                SELECT id, created_at, material_code, supplier, material_function,
                       process_name, document_type, original_filename, storage_backend
                FROM uploads
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
        )


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


def db_get(upload_id: int) -> sqlite3.Row | None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM uploads WHERE id = ?", (upload_id,)).fetchone()
        return row


def select_options(options: list[str], selected: str = "") -> str:
    parts = []
    for option in options:
        attr = " selected" if option == selected else ""
        parts.append(f'<option value="{html.escape(option)}"{attr}>{html.escape(option)}</option>')
    return "\n".join(parts)


def render_recent_rows(admin_token: str) -> str:
    rows = db_recent()
    if not rows:
        return '<tr><td colspan="10" class="muted">还没有上传记录。</td></tr>'

    rendered = []
    for row in rows:
        download = (
            f'<a href="/download?id={row["id"]}&token={quote(admin_token)}">下载</a>'
            if row["storage_backend"] == "local"
            else ""
        )
        rendered.append(
            "<tr>"
            f"<td>{row['id']}</td>"
            f"<td>{html.escape(row['created_at'])}</td>"
            f"<td>{html.escape(row['material_code'])}</td>"
            f"<td>{html.escape(row['supplier'])}</td>"
            f"<td>{html.escape(row['material_function'])}</td>"
            f"<td>{html.escape(row['process_name'])}</td>"
            f"<td>{html.escape(row['document_type'])}</td>"
            f"<td>{html.escape(row['original_filename'])}</td>"
            f"<td>{html.escape(row['storage_backend'])}</td>"
            f"<td>{download}</td>"
            "</tr>"
        )
    return "\n".join(rendered)


def render_page(message: str = "", error: str = "", token: str = "") -> bytes:
    message_html = f'<div class="notice success">{html.escape(message)}</div>' if message else ""
    error_html = f'<div class="notice error">{html.escape(error)}</div>' if error else ""
    token_input = html.escape(token)
    token_block = ""
    if UPLOAD_TOKEN:
        token_block = f"""
        <label>
          上传口令
          <input type="password" name="token" value="{token_input}" placeholder="请输入甲方提供的上传口令" required>
        </label>
        """

    body = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>依耐克物料资料上传</title>
  <link rel="stylesheet" href="/static/styles.css">
</head>
<body>
  <main>
    <section class="hero">
      <div>
        <h1>依耐克物料资料上传</h1>
        <p>请上传 PDF 格式的 TDS、SDS/MSDS 或检测资料。一个文件提交一次；同一物料可重复提交多个资料。</p>
      </div>
      <div class="status">
        <span>PDF only</span>
        <strong>最大 {MAX_UPLOAD_MB} MB</strong>
      </div>
    </section>

    {message_html}
    {error_html}

    <section class="panel">
      <form action="/upload" method="post" enctype="multipart/form-data">
        {token_block}
        <div class="grid">
          <label>
            物料型号/牌号
            <input name="material_code" placeholder="例如 EC-GM62-C20-57564" required>
          </label>
          <label>
            供应商/品牌
            <input name="supplier" placeholder="供应商名称" required>
          </label>
          <label>
            物料功能/作用
            <select name="material_function" required>
              <option value="">请选择</option>
              {select_options(FUNCTION_TYPES)}
            </select>
            <input class="other-input" data-other-for="material_function" name="material_function_other" placeholder="选择其他时，请填写具体功能/作用">
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
            <input name="substrate" placeholder="例如 SUS、不锈钢、铝合金" required>
          </label>
          <label>
            适用工艺/工序
            <select name="process_name" required>
              <option value="">请选择</option>
              {select_options(PROCESS_TYPES)}
            </select>
            <input class="other-input" data-other-for="process_name" name="process_name_other" placeholder="选择其他时，请填写具体工艺/工序">
          </label>
        </div>

        <label>
          PDF 文件
          <input type="file" name="file" accept="application/pdf,.pdf" required>
        </label>

        <label>
          备注
          <textarea name="note" rows="3" placeholder="例如：该资料无推荐固化条件；配比需现场试验确认"></textarea>
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


def render_admin_page(admin_token: str) -> bytes:
    body = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>上传记录后台</title>
  <link rel="stylesheet" href="/static/styles.css">
</head>
<body>
  <main>
    <section class="hero">
      <div>
        <h1>上传记录后台</h1>
        <p>仅供甲方内部查看。不要把本页面链接发给供应商。</p>
      </div>
    </section>
    <section class="panel">
      <div class="section-title">
        <h2>最近上传</h2>
        <span>按上传时间倒序显示</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>时间</th>
              <th>物料型号</th>
              <th>供应商</th>
              <th>功能</th>
              <th>工艺</th>
              <th>资料类型</th>
              <th>文件名</th>
              <th>存储</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {render_recent_rows(admin_token)}
          </tbody>
        </table>
      </div>
    </section>
  </main>
</body>
</html>"""
    return body.encode("utf-8")


class UploadHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            token = parse_qs(parsed.query).get("token", [""])[0]
            self.send_html(render_page(token=token))
            return
        if parsed.path == "/admin":
            self.handle_admin(parsed.query)
            return
        if parsed.path == "/static/styles.css":
            self.send_static_css()
            return
        if parsed.path == "/download":
            self.handle_download(parsed.query)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/upload":
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        try:
            self.handle_upload()
        except Exception as exc:
            self.send_html(render_page(error=str(exc)), status=HTTPStatus.BAD_REQUEST)

    def handle_admin(self, query: str) -> None:
        if not ADMIN_TOKEN:
            self.send_error(HTTPStatus.NOT_FOUND, "Admin page disabled")
            return
        admin_token = parse_qs(query).get("token", [""])[0]
        if admin_token != ADMIN_TOKEN:
            self.send_error(HTTPStatus.FORBIDDEN, "Forbidden")
            return
        self.send_html(render_admin_page(admin_token))

    def handle_upload(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            raise ValueError("没有收到上传内容")
        if content_length > MAX_UPLOAD_MB * 1024 * 1024:
            raise ValueError(f"文件超过限制：最大 {MAX_UPLOAD_MB} MB")

        body = self.rfile.read(content_length)
        fields, files = parse_multipart(self.headers.get("Content-Type", ""), body)

        if UPLOAD_TOKEN:
            token = get_text(fields, "token")
            if token != UPLOAD_TOKEN:
                raise ValueError("上传口令错误")

        material_code = require_text(fields, "material_code", "物料型号/牌号")
        supplier = require_text(fields, "supplier", "供应商/品牌")
        material_function = resolve_other_choice(fields, "material_function", "物料功能/作用")
        substrate = require_text(fields, "substrate", "适用基材")
        process_name = resolve_other_choice(fields, "process_name", "适用工艺/工序")
        document_type = resolve_other_choice(fields, "document_type", "资料类型")
        note = get_text(fields, "note")

        file_item = files.get("file")
        if file_item is None or not file_item.get("filename"):
            raise ValueError("请选择 PDF 文件")

        original_filename = Path(str(file_item["filename"])).name
        if not original_filename.lower().endswith(".pdf"):
            raise ValueError("仅允许上传 PDF 文件")

        content = file_item["content"]
        if not isinstance(content, bytes):
            raise ValueError("文件读取失败")
        if not content.startswith(b"%PDF"):
            raise ValueError("文件内容不像有效 PDF，请确认后重新上传")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique = uuid.uuid4().hex[:8]
        safe_filename = f"{timestamp}_{unique}_{safe_segment(original_filename, 'document.pdf')}"
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
        self.send_html(render_page(message=f"上传成功，记录 ID：{upload_id}"))

    def handle_download(self, query: str) -> None:
        params = parse_qs(query)
        if not ADMIN_TOKEN:
            self.send_error(HTTPStatus.NOT_FOUND, "Download disabled")
            return
        admin_token = params.get("token", [""])[0]
        if admin_token != ADMIN_TOKEN:
            self.send_error(HTTPStatus.FORBIDDEN, "Forbidden")
            return
        upload_id_text = params.get("id", [""])[0]
        if not upload_id_text.isdigit():
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing id")
            return
        row = db_get(int(upload_id_text))
        if not row:
            self.send_error(HTTPStatus.NOT_FOUND, "Upload not found")
            return
        path = Path(row["stored_path"])
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "Local file not found")
            return
        data = path.read_bytes()
        filename = quote(row["original_filename"])
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{filename}")
        self.end_headers()
        self.wfile.write(data)

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

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), fmt % args))


def main() -> None:
    init_storage()
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), UploadHandler)
    print(f"Yinaike material upload portal: http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
