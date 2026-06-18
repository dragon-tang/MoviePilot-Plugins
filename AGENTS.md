# MoviePilot-Plugins

Dragon 维护的 MoviePilot 插件仓库。当前只在**本 Telegram 话题**推进本仓库相关工作；旧 302 插件话题只用于 `session_search` 查历史，不再在那里继续提交或讨论，避免冲突。

## 当前插件

```text
plugins.v2/mediaservermsgai/      # 媒体库服务器通知AI版
plugins.v2/embyreverseproxyai/    # Emby 302 反向代理AI版
package.v2.json                   # 插件市场元数据 + 版本历史
```

## Git / 推送规则

本地工作目录：

```text
/tmp/MoviePilot-Plugins
```

本仓库使用项目专用 Git token helper：

```text
/root/.config/git-credentials-moviepilot
```

不要写入或修改全局 `gh` 登录。若 `git push` 返回 403，优先检查该 PAT 是否对 `dragon-tang/MoviePilot-Plugins` 开启 `Contents: Read and write`。

推送前：

```bash
git status -sb
python3 -m json.tool package.v2.json >/dev/null
python3 -m py_compile plugins.v2/mediaservermsgai/__init__.py \
  plugins.v2/embyreverseproxyai/__init__.py \
  plugins.v2/embyreverseproxyai/proxy_app.py \
  plugins.v2/embyreverseproxyai/external_players.py
git pull --rebase origin main
git push origin main
```

遇到冲突用 `git pull --rebase`，不要 force push，除非用户明确授权。

## package.v2.json 规则

- 只能**增量合并**插件条目，绝不能用单插件 `package.v2.json` 覆盖仓库原文件。
- 保留已有插件，例如 `MediaServerMsgAI` 和 `EmbyReverseProxyAI`。
- 改插件版本时，同步更新：
  - `plugins.v2/<plugin>/__init__.py` 的 `plugin_version`
  - `package.v2.json` 对应插件的 `version`
  - `package.v2.json` 对应插件的 `history`
- 没有正式 release 流程时，不写 `"release": true`。

## MediaServerMsgAI 规则

### 设计约定

- 路径关键词黑名单 + TMDB 未识别过滤导致通知被吞，是用户特意设计的功能，不要当作误杀修复。
- 原始 webhook JSON 的 debug 日志是调试用途，保留。
- `user.authenticated` 登录成功和 `user.authenticationfailed` 登录失败必须是配置表单中两个独立选项，不能合并。
- 仪表盘已移除：`get_page()` 保持 `return []`，不要重建。

### 配置页

- 使用 Vuetify 组件树。
- 双栏布局：左栏「基本设置 / 入库设置」，右栏「过滤设置 / 显示设置」。
- 控件尽量使用 `density='compact'`、`hide-details='auto'`。
- 修改 `get_form` 时只用小范围 `patch`，不要用大段正则 / 字符串重写，避免括号错配。

### 消息限制

深度删除消息：媒体名称 120 字符，路径 300 字符，挂载路径单条 200 字符，最多显示 5 条。

## EmbyReverseProxyAI 规则

### 稳定基线

- 当前稳定基线：`0.2.12`。
- `external_players.py` / forward 逻辑保持 DDSRem-Dev 上游原始实现，除非用户明确要求，不要擅改。
- 原作者代码里无关当前需求的结构不要随意改，例如 `get_command()` / `get_page()` 的 `pass` 保持原样。

### 用户确认过的设计

- 完整 302 真实直链 / token 参数日志是调试用途，保留。
- `/redirect2external` 解码后直接 302 到任意地址是设计，保留。
- 真实 IP 只转发给 Emby，不影响 302 直链解析。
- 地区拦截和客户端 + DeviceId 白名单只作用于登录 / 认证接口，避免影响 302 播放速度。
- 客户端白名单支持备注，匹配时剥离备注。

### AI 版共存要求

若导入或改造 AI 版插件，需要同步这些标识，避免和上游冲突：

```text
folder: plugins.v2/embyreverseproxyai
package key: EmbyReverseProxyAI
class: EmbyReverseProxyAI
plugin_config_prefix: embyreverseproxyai_
```

显示名可以叫 AI 版，但不要随意改压缩包 / 项目内部路径，除非用户明确要求。

## 编辑纪律

- 小步 patch，每次关键修改后立刻 `py_compile`。
- 复杂 UI 嵌套结构不要用正则整体替换。
- 修改远程正在运行的服务脚本时，先备份原文件，再重启服务并检查状态与日志。
- 不要把用户明确说明“正常 / 设计如此”的行为再次列为 bug。
