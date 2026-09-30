---
name: academic-paper-search
description: 知网学术论文搜索与 PDF 下载。支持关键词检索、时间范围/期刊来源/数据库筛选、结果预览、单条或批量下载 PDF；内置 Cookie 加密管理（首次引导录入、定期有效性检测、失效自动引导重填）。当用户要求搜学术论文、查文献、下载文献 PDF、管理知网 Cookie 时使用。不用于非学术网页抓取。
---

# 学术论文搜索（知网）

基于已逆向的 CNKI kns8s 新版接口（总库口径 + 动态签名 + 翻页惯例），提供文献检索与 PDF 下载。skill 自包含：核心逻辑在 `scripts/cnki_core.py`（检索签名/QueryJson/grid 解析/翻页/反爬检测/直连下载/CAJ→PDF 转换），不依赖 zhiwang 项目，任意目录均可执行。依赖 requests、beautifulsoup4，CAJ 转换另需 PyPDF2。

## 工作流（每次会话首次使用时按此顺序）

1. **检查 Cookie 状态**：
   ```bash
   python3 .trae/skills/academic-paper-search/scripts/cookie_manager.py check
   ```
   - 输出 `VALID` → 继续第 2 步
   - 输出 `MISSING`（从未配置）→ 走下方「首次引导流程」
   - 输出 `INVALID <原因>`（已失效）→ 把失效原因告诉用户，然后走「首次引导流程」重新录入
2. **搜索文献**：
   ```bash
   python3 .trae/skills/academic-paper-search/scripts/paper_search.py --kw "关键词" [筛选参数] --save /tmp/result.json
   ```
3. **预览结果**：读取 `--save` 的 JSON，向用户展示表格（序号/题名/作者/来源/发表时间/数据库/被引/下载）。行内有 `下载链接` 字段即可下载。
4. **下载 PDF**：让用户选择序号（如 "1,3-5"）后执行：
   ```bash
   python3 .trae/skills/academic-paper-search/scripts/paper_download.py --input /tmp/result.json --sel 1,3-5 --out ~/Downloads/papers
   ```
   下载优先级：详情页『PDF下载』按钮的原生 PDF > 直连 PDF > CAJ 落盘后自动转 PDF（转换失败才保留 .caj）。

## 首次引导流程（Cookie 录入）

1. 把 [cookie-guide.md](references/cookie-guide.md) 的步骤展示给用户：登录 sclib.cn 机构入口 → 打开检索页 → F12 复制 Cookie。
2. 让用户直接把 Cookie 文本粘贴到对话里（格式见指南，`name=value; name2=value2` 串或 JSON 数组均可）。
3. 将用户粘贴内容写入临时文件后执行：
   ```bash
   python3 .../scripts/cookie_manager.py init --file /tmp/cookie.txt
   ```
   脚本会自动识别格式、加密存储（AES-Fernet，密钥文件权限 600）、并立即验证一次。
4. 验证失败时脚本会给出原因（见错误处理表），修复后重新录入。

## paper_search.py 参数

| 参数 | 说明 | 示例 |
|------|------|------|
| `--kw` | 检索词（必填） | `--kw "大模型 应急管理"` |
| `--field` | 检索字段：SU 主题(默认)/TI 篇名/AB 摘要/KY 关键词/AU 作者 | `--field TI` |
| `--start-year` / `--end-year` | 发表年度范围 | `--start-year 2020 --end-year 2026` |
| `--source-cats` | 来源类别多选：核心期刊=NCPCJ,北大核心=PT,CSSCI=CSI,CSCD=CSD,EI=EI | `--source-cats CSI,PT` |
| `--dbs` | 数据库多选（期刊/学位论文/会议/报纸/图书/特色期刊/科技成果），默认总库 | `--dbs 期刊,报纸` |
| `--sort` | 排序：相关度(默认)/时间/被引/下载/综合 | `--sort 被引` |
| `--page` / `--page-size` | 分页（默认 1 / 20） | |
| `--save` | 结果保存为 JSON（推荐，供下载脚本使用） | |

## 错误处理表（必须向用户明确提示原因与解决方案）

| 场景 | 表现 | 处理 |
|------|------|------|
| Cookie 失效 | check 返回 INVALID，检索报 BlockedError / elib 挑战页 | 告知用户失效原因（过期/被服务端风控），按引导流程重新导出粘贴 |
| 下载跳登录页 | 下载报 INVALID_COOKIE（下载链接重定向到 login--cnki--net 登录页） | 下载通道要求登录：按引导流程重新导出 Cookie（重登 sclib.cn 后立刻复制）再试；个别文献仍失败则属账号权限问题，跳过即可 |
| 网络异常 | 脚本自动重试 3 次（指数退避）后仍失败 | 提示检查网络或 sclib.cn 可达性，稍后重试 |
| 下载失败 | 单条下载返回错误 | 报告具体文献与原因；该文献可能无下载权限，建议跳过或换渠道 |
| 无结果 | total=0 | 建议放宽筛选（去掉年份/来源限制）或更换检索词 |
| 触发验证码 | 连续高频请求 | 立即停止批量下载，等 10 分钟再继续 |

## 安全说明

- Cookie 以 Fernet 对称加密存储于 `~/.academic-paper-search/cookie.enc`，密钥文件 `key.bin` 权限 0600，仅本机当前用户可读。
- 绝不把 Cookie 明文写入日志、终端输出或 git 提交。
- `~/.academic-paper-search/` 已由脚本确保权限；如需彻底清除：删除该目录即可。
