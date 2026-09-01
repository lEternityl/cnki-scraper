# CNKI 抓取与检索系统

基于 FastAPI 的中国知网（CNKI）文献抓取、检索、统计与 PDF 全文下载的本地 Web 系统。提供可视化界面，支持按期刊批量抓取题录、高级检索、数据统计可视化以及全文（CAJ/PDF）批量下载，所有任务均通过 Server-Sent Events（SSE）实时推送日志。

## 系统总览

启动后访问 http://127.0.0.1:8000 即可打开 Web 控制台。顶部导航栏提供六个功能模块，覆盖从数据采集到全文下载的完整流程。

![系统主界面](docs/scrape.png)

## 功能特性

### 1. 抓取任务

按期刊列表批量抓取 CNKI 文献题录（篇名、作者、刊名、发表时间、关键词、摘要等），适合构建本地文献语料库。

![抓取任务](docs/scrape.png)

**可配置参数：**
- **期刊列表**：每行一个期刊名，留空则使用内置的 23 个 CSSCI/核心期刊默认列表（含中国工业经济、管理世界、经济研究等）。
- **起始年 / 结束年**：抓取的年份区间，默认 2010–2019。
- **每页条数**：单次翻页请求的条数，默认 50。
- **最大页数**：限制每个期刊最多抓取多少页，0 表示不限。
- **最小/最大间隔（秒）**：每次请求的随机睡眠区间，规避反爬。
- **清空已有进度**：勾选后会重置断点状态，重新从第一页开始抓取。

**运行时展示：** 启动后右侧实时日志区通过 SSE 滚动输出抓取进度；左侧状态卡片显示当前期刊、页码进度、本任务已写记录数、已耗时与错误信息。支持断点续抓——中断后再次启动会从上次记录的期刊与页码继续。

### 2. 文献检索

对已抓取到本地的题录进行多维分页检索。

![文献检索](docs/records.png)

**检索条件：** 关键词（在篇名/作者/摘要/关键词中匹配）、期刊、作者、年份，可任意组合。支持每页 10/20/50/100 条切换。

**结果展示：** 表格列出篇名、作者、刊名、发表时间、关键词；**点击任意行可展开**查看检索期刊、摘要全文与原文链接。底部提供分页导航（上一页/下一页及页码）。

### 3. 高级检索（CNKI 实时）

与 CNKI `kns8s/AdvSearch` 接口一致的实时高级检索，不依赖本地数据，直接查询知网。

![高级检索](docs/advsearch.png)

**检索能力：**
- **多条件组合**：可添加任意数量的检索条件行，行间用 AND / OR / NOT 逻辑连接（首行逻辑固定为 AND）。
- **检索字段**：主题、篇名、关键词、作者、作者单位、来源期刊、摘要、基金、中图分类号、DOI、参考文献。
- **来源类别筛选**：CSSCI、北大核心、SCI/EI/SSCI/AHCI、CSCD、CSTPCD，可多选。
- **年代范围**：自定义起止年。

**两种使用方式：**
- **检索**：单页实时查询，结果表格显示篇名（带原文链接）、作者、刊名、发表时间，支持翻页。
- **采到本地**：启动后台采集任务，按当前条件分页抓取全部结果并合并写入本地 records，可设置最大页数与抓取间隔，下方实时显示采集进度与日志。

> 注：CNKI 翻页必须携带前一页返回的 turnpage 令牌，系统按查询签名缓存会话链以支持任意页跳转；上传新 Cookie 后缓存自动失效。

### 4. 统计可视化

对本地已抓取记录进行聚合统计，以条形图直观展示分布。

![统计可视化](docs/stats.png)

**四个维度统计：**
- 按期刊（Top 20）
- 按年份
- 高产作者（Top 20）
- 高频关键词（Top 20）

每条统计以横向条形图呈现，条长度按计数占比，右侧标注具体数量。点击「刷新统计」可重新计算。

### 5. 文献下载

在已抓取记录中按条件筛选文献，批量下载全文（CAJ/PDF）。

![文献下载](docs/pdf.png)

**筛选条件：** 关键词、期刊、作者、年份，留空表示全部。可配置最小/最大下载间隔，以及是否覆盖已存在文件。

**下载策略：** 优先使用 grid 列表中的下载链接（多为 CAJ 格式）；老记录若无下载链接，则回退到详情页解析（可能触发验证码，需有效 Cookie）。

**运行时展示：** 状态卡片显示进度（已下载/跳过/失败/总数及百分比）、当前处理标题、错误信息；右侧实时日志区滚动输出每条下载结果。下方「已下载文件」列表展示文件名、大小、下载时间，并提供下载链接。

### 6. Cookie 管理

CNKI 抓取与下载均需登录态，本模块统一管理 Cookie 文件。

![Cookie 管理](docs/cookie.png)

**功能：** 上传 `cnki_cookies.json`（浏览器导出的 Cookie）、查看状态（是否已上传、条目数、所属域名）、删除 Cookie。上传后会自动使检索会话缓存失效，确保使用新会话。

## 技术栈

| 层 | 技术 |
| ---- | ---- |
| 后端 | FastAPI + Uvicorn |
| HTTP/解析 | requests + BeautifulSoup4 |
| PDF 处理 | PyPDF2 |
| 前端 | 原生 HTML / CSS / JavaScript（无构建步骤） |
| 实时日志 | Server-Sent Events（SSE） |
| 数据存储 | JSON / NDJSON（文件存储，无需数据库） |

## 项目结构

```
zhiwang/
├── backend/                 # FastAPI 后端
│   ├── main.py              # 应用入口与全部 API 路由
│   ├── scraper.py           # 按期刊抓取、高级检索采集与 turnpage 会话管理
│   ├── pdf_downloader.py    # PDF/CAJ 全文下载
│   └── storage.py           # JSON/NDJSON 存储与检索、Cookie 管理
├── static/                  # 前端静态资源
│   ├── index.html           # 单页控制台
│   ├── app.js               # 各 Tab 渲染与事件逻辑
│   └── style.css            # 样式
├── docs/                    # 文档与截图
│   ├── screenshot.png
│   ├── scrape.png
│   ├── records.png
│   ├── advsearch.png
│   ├── stats.png
│   ├── pdf.png
│   └── cookie.png
├── requirements.txt
├── start.sh                 # 后台启动脚本
├── stop.sh                  # 停止脚本
└── .gitignore
```

## 安装与运行

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

依赖清单：`fastapi`、`uvicorn[standard]`、`requests`、`beautifulsoup4`、`python-multipart`、`PyPDF2`。

### 2. 准备 Cookie

从浏览器导出 CNKI 登录后的 Cookie 为 `cnki_cookies.json`（放在项目根目录），或启动后通过界面「Cookie 管理」标签页上传。抓取与下载均依赖有效 Cookie。

### 3. 启动服务

后台启动（推荐）：

```bash
./start.sh
```

前台运行（便于调试）：

```bash
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

启动成功后访问：http://127.0.0.1:8000

### 4. 停止服务

```bash
./stop.sh
```

## 主要 API

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| GET | `/api/health` | 健康检查 |
| GET | `/api/journals` | 默认与已抓取期刊列表 |
| GET | `/api/records` | 本地记录分页检索 |
| GET | `/api/records/stats` | 本地记录聚合统计 |
| GET | `/api/search/fields` | 检索字段与来源类别元数据 |
| POST | `/api/search` | 单页实时高级检索 |
| POST | `/api/search/collect` | 启动后台采集任务 |
| GET | `/api/search/collect/status` | 采集任务状态 |
| GET | `/api/search/collect/logs` | 采集实时日志（SSE） |
| POST | `/api/scrape/start` | 启动按期刊抓取 |
| POST | `/api/scrape/stop` | 停止抓取 |
| GET | `/api/scrape/status` | 抓取任务状态 |
| GET | `/api/scrape/logs` | 抓取实时日志（SSE） |
| POST | `/api/pdf/start` | 启动全文下载 |
| POST | `/api/pdf/stop` | 停止下载 |
| GET | `/api/pdf/status` | 下载任务状态 |
| GET | `/api/pdf/files` | 已下载文件列表 |
| GET | `/api/pdf/files/{name}` | 下载指定文件 |
| GET / POST / DELETE | `/api/cookie` | Cookie 查询 / 上传 / 删除 |

## 数据存储

所有抓取产物默认写入项目根目录下的 `cnki_2010_2019/`（路径可在 `backend/storage.py` 调整）：

| 文件 | 说明 |
| ---- | ---- |
| `cnki_records.ndjson` | 题录主数据（每行一条 JSON，增量写入，断点续抓友好） |
| `cnki_records.json` | 题录全量 JSON（供前端浏览/统计） |
| `cnki_scrape_state.json` | 抓取进度状态（期刊/页码断点） |
| `cnki_scrape_summary.json` | 抓取汇总 |
| `pdfs/` | 下载的全文文件（CAJ/PDF） |

## 注意事项

- 抓取与下载行为需遵守 CNKI 的使用条款，请合理控制请求频率（系统默认已内置随机间隔）。
- `cnki_cookies.json` 含登录凭证，已加入 `.gitignore`，切勿提交到版本库。
- CNKI 翻页依赖前一页返回的 turnpage 令牌，高级检索按查询签名缓存会话以支持任意页跳转。
- 当 CNKI 触发安全验证时，系统会抛出 `BlockedError`，前端提示重新上传 Cookie。
