# 依耐克物料资料上传门户

这是一个供应商资料上传页面。供应商打开网页后填写物料信息，选择资料类型，并上传 PDF 格式的 TDS、SDS/MSDS 或检测资料。

当前方案：

- 不使用 OSS。
- 不设置上传口令。
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

PDF 文件保存到：

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
| `UPLOAD_DATA_DIR` | 上传文件和数据库保存目录，默认 `./data` |
| `MAX_UPLOAD_MB` | 单个 PDF 最大大小，默认 `30` |

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
      SDS/
```

## 供应商填写字段

- 物料型号/牌号
- 供应商/品牌
- 物料功能/作用
- 资料类型：TDS / SDS / MSDS / COA/检测报告 / 其他
- 适用基材
- 适用工艺/工序
- PDF 文件
- 备注

如果下拉选择“其他”，页面会显示手动填写框，并要求供应商填写具体内容。

## 注意

- 当前只允许上传 PDF。
- 供应商端不会展示最近上传记录，避免不同供应商互相看到资料。
- 公网部署时，服务器路径不要暴露 `data/` 目录。
- 服务器应定期备份 `UPLOAD_DATA_DIR`，至少备份 `uploads.db` 和 `files/`。
