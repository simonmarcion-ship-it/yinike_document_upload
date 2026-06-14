# 依耐克物料资料上传门户

这是一个供应商资料上传页面。供应商打开网页后填写物料信息，选择资料类型，并上传 TDS、SDS/MSDS、检测报告、图片、表格或其他资料文件。

当前方案：

- 不使用 OSS。
- 设置上传密码，默认 `20250605`。
- 不开放后台列表和下载页面。
- 供应商端只负责上传，不展示任何历史上传记录。
- 所有文件和表单记录都保存在部署服务器上。

## 文件和表单如何对应

每次上传会写入一条 SQLite 记录：

```text
data/uploads.db
```

这条记录同时保存：

- 物料型号/牌号
- 供应商/品牌
- 物料功能/作用
- 适用基材
- 适用工艺/工序
- 资料类型
- 原始文件名
- 服务器本地文件路径
- 上传时间
- 上传备注

上传文件保存到：

```text
data/files/
```

因此对应关系由数据库记录维护，不靠文件名猜。

## 运行

```powershell
cd C:\Users\78535\.codex\skills\yinike_material_upload_portal
python app.py
```

供应商访问：

```text
http://服务器IP:8080/
```

如果通过 nginx 挂到域名子路径，例如：

```text
https://www.hajimitech.com/yinike/upload_document/
```

则需要在 `.env` 中设置：

```text
BASE_PATH=/yinike/upload_document
```

本机测试：

```text
http://127.0.0.1:8080/
```

## 环境变量

可复制 `.env.example` 作为部署参考。

| 变量 | 说明 |
|---|---|
| `HOST` | 默认 `0.0.0.0` |
| `PORT` | 默认 `8080` |
| `BASE_PATH` | 部署到域名子路径时使用，例如 `/yinike/upload_document`；直接用端口访问时留空 |
| `UPLOAD_DATA_DIR` | 上传文件和数据库保存目录，默认 `./data` |
| `MAX_UPLOAD_MB` | 单个文件最大大小，默认 `30` |
| `UPLOAD_PASSWORD` | 上传页面访问密码，默认 `20250605` |
| `INTERNAL_PASSWORD` | 内部物料维护页面访问密码，默认 `20250605`；建议上线后与供应商上传密码分开 |
| `MINERU_API_KEY` | MinerU API key；为空时内部文件只保存，不提交解析 |
| `MINERU_AUTO_PARSE` | 是否自动提交 MinerU 解析，默认 `false`；当前阶段只收原件，保持关闭 |
| `MINERU_MODEL_VERSION` | MinerU 解析模型，默认 `vlm` |
| `MINERU_LANGUAGE` | MinerU 文档语言，默认 `ch` |
| `MINERU_MAX_WAIT_SECONDS` | 上传后同步等待解析结果的秒数，默认 `12`；超时后可在页面刷新状态 |

## Docker 部署

推荐使用 Docker Compose。先复制环境变量文件：

```powershell
Copy-Item .env.example .env
```

启动：

```powershell
docker compose up -d --build
```

查看日志：

```powershell
docker compose logs -f
```

停止：

```powershell
docker compose down
```

### nginx 子路径反向代理

如果要让供应商访问：

```text
https://www.hajimitech.com/yinike/upload_document/
```

先在服务器项目目录的 `.env` 中设置：

```text
BASE_PATH=/yinike/upload_document
PORT=8080
UPLOAD_PASSWORD=20250605
INTERNAL_PASSWORD=20250605
MINERU_API_KEY=填你的_MinerU_API_Key
MINERU_AUTO_PARSE=false
MINERU_MODEL_VERSION=vlm
MINERU_LANGUAGE=ch
```

然后在 `/www/server/panel/vhost/nginx/www.hajimitech.com.conf` 的 `server { ... }` 内增加：

```nginx
location = /yinike/upload_document {
    return 301 /yinike/upload_document/;
}

location ^~ /yinike/upload_document/ {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;

    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    client_max_body_size 100m;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
}
```

这里的 `proxy_pass` 不要在 `8080` 后面加 `/`，否则后端收到的路径会被 nginx 改写，子路径部署会出问题。`^~` 用来避免宝塔默认静态文件规则抢走 CSS/JS 请求。

备选：直接 Docker 运行：

```powershell
docker build -t yinike-material-upload .
docker run -d --name yinike-material-upload ^
  -p 8080:8080 ^
  -v C:\yinike_upload_data:/app/data ^
  yinike-material-upload
```

关键点是挂载 `/app/data` 或使用 compose 中的 `./data:/app/data`。如果不挂载，容器重建后上传文件和数据库会丢失。

## 数据保存结构

```text
data/
  uploads.db
  files/
    EC-GM62-C20-57564/
      TDS/
        20260605_153000_xxxxxxxx_supplier_file.pdf
        20260605_153100_xxxxxxxx_supplier_file.xlsx
        20260605_153200_xxxxxxxx_supplier_image.jpg
      SDS/
  internal_files/
    AAA0010006/
      TDS（产品技术资料）/
        20260606_120000_xxxxxxxx_internal_file.pdf
  mineru_results/
    12/
      20260606_120010_result.json
      mineru_result.zip
      full.md
```

## 内部物料维护页面

内部人员访问：

```text
https://service.hajimitech.com/yinike/upload_document/internal/materials/
```

内部页面可以：

- 上传并同步 ERP 导出的物料编码列表 `.xlsx`。
- 按物料编号、物料名称、规格搜索。
- 展示 ERP 基础字段：序号、物料编号、物料名称、规格型号、单位、类别、供应商编号、最低警戒数、最高警戒数。
- 对每个物料补充用途/作用、适用工序、内部备注。
- 对每个物料上传资料原件。
- 每次上传形成一条文件记录，页面可显示文件名、下载原件，并给该文档单独填写备注。
- 当前阶段默认不做识别；`MINERU_AUTO_PARSE=false` 时不会提交 MinerU。

导入 ERP 清单只会新增/更新 ERP 基础字段，已经人工补录的用途、工序、备注和文件会保留。新清单里不存在的旧物料不会删除，会作为“旧清单保留”继续显示，便于回溯。

如果同一个物料编号在新旧 ERP 清单中的物料名称、规格、单位、类别、供应商编号等基础字段不一致，系统会阻止本次导入，并列出冲突物料和冲突字段。必须人工核对并修改 ERP 清单或既有记录后，才能重新导入。

最近一次 ERP 导入冲突会保留在内部页面顶部，搜索物料或刷新页面不会消失；处理完成后可以点击“清除提示”，成功同步 ERP 清单也会自动清除旧冲突提示。

## 供应商填写字段

- 物料型号/牌号
- 供应商/品牌
- 物料功能/作用
- 资料类型：TDS / SDS / MSDS / COA/检测报告 / 其他
- 适用基材
- 适用工艺/工序
- 资料文件
- 备注

如果下拉选择“其他”，页面会显示手动填写框，并要求供应商填写具体内容。

## 注意

- 当前允许上传任意资料文件。不要把 `data/` 目录作为静态目录公开。
- 供应商端不会展示最近上传记录，避免不同供应商互相看到资料。
- 公网部署时，服务器路径不要暴露 `data/` 目录。
- 服务器应定期备份 `UPLOAD_DATA_DIR`，至少备份 `uploads.db`、`files/`、`internal_files/` 和 `mineru_results/`。
