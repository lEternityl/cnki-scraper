# 知网 Cookie 获取与格式说明

## 为什么需要 Cookie

本技能通过 sclib.cn（四川省图书馆）机构代理访问知网总库，检索与 PDF 下载都需要登录态 Cookie。Cookie 过期或被服务端风控时需重新导出。

## 获取步骤（Chrome / Edge / Trae 内置浏览器均可）

1. 打开 `https://www.sclib.cn/`，找到「知网」数据库入口（数字资源 / 数据库检索），点击后按机构方式登录（如读者证号）。无四川省图书馆账号时可改用其它图书馆代理入口，但代理域名需相应修改。
2. 登录成功后会跳转到知网检索页（域名形如 `kns--cnki--net.share.sclib.cn`）。在该页面上按 `F12`（或右键 → 检查）打开开发者工具。
3. 切到 **Network（网络）** 标签，刷新页面（Cmd+R）。
4. 点击第一条请求（就是当前页面地址），在右侧 **Request Headers（请求标头）** 里找到 `Cookie:` 一行，**完整复制**它的值（一行超长的 `k1=v1; k2=v2; ...` 串）。

## 格式

两种格式录入脚本都能识别：

- **Cookie 请求头串**（推荐，最简单）：
  ```
  Ecp_session=1; LID=WEEvREcw...$9A4hF_YAuv...; Ecp_LoginStuts={"IsAutoLogin":true,...}; SID_kns_new=kns2618133; ...
  ```
- **scrapy 格式 JSON 数组**（从浏览器插件 Cookie 导出器导出的格式）：
  ```json
  [{"domain": ".share.sclib.cn", "name": "Ecp_session", "value": "1"}, ...]
  ```

## 录入

把复制到的 Cookie 文本发给对话里的助手（直接粘贴即可），助手会保存为临时文件并执行：

```bash
python3 .trae/skills/academic-paper-search/scripts/cookie_manager.py init --file /tmp/cookie.txt
```

脚本自动识别格式 → 加密存储（`~/.academic-paper-search/cookie.enc`，密钥文件权限 600）→ 立即验证。

## 有效性判断要点

- 必须包含登录态字段：`Ecp_session`、`LID`（或 `Ecp_LoginStuts`）。只有 `SID_sug` 之类的匿名字段说明未登录。
- 常见失效原因：
  - **自然过期**：会话有效期一般数小时到数天
  - **服务端风控**：高频请求后 sclib 代理会要求 elib 安全验证，此时必须重新走浏览器登录流程
  - **库端下线**：机构代理服务维护

## 安全提示

- Cookie 加密存储于本机 `~/.academic-paper-search/`，不会进入 git 仓库
- 不要把 Cookie 发给不可信的第三方或粘贴到公开场合（它等同于你的图书馆登录态）
- 彻底清除：`python3 .trae/skills/academic-paper-search/scripts/cookie_manager.py clear`
