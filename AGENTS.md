# MoviePilot-Plugins

Dragon 维护的 MoviePilot 插件仓库。
唯一插件：`MediaServerMsgAI`（媒体库服务器通知AI增强版）。

## 项目结构

```
plugins.v2/mediaservermsgai/
├── __init__.py        # 主插件源码
├── requirements.txt   # 依赖
package.v2.json        # 插件元数据 + 版本历史
```

## 版本管理规则

每次代码修改必须同步更新以下两处：

```yaml
# __init__.py 第55行
plugin_version = "2.X.X"

# package.v2.json
version: "2.X.X"
history:
  v2.X.X: "对应变更说明"
```

- 版本号按语义递增（大改+1，修复+1）
- `package.v2.json` 的 `history` 条目用 `OrderedDict` 保持顺序，新版本在最前面

## 提交规范

```
type(scope): 中文描述 (vX.X.X)
```

| 类型 | 使用场景 |
|------|----------|
| `feat` | 新功能、UI改动 |
| `fix` | Bug修复 |
| `refactor` | 代码重构，无功能变化 |
| `chore` | 杂项（清理历史、格式化等） |

示例：
```
feat(UI): 状态改回纯文本，仪表盘布局紧凑化 (v2.1.4)
fix(UI): 恢复配置页，仅移除仪表盘 (v2.1.6)
chore: 精简package.v2.json历史记录，仅保留v2.0.0+版本
```

## 推送流程

```bash
git add -A
git commit -m "type(scope): 描述 (vX.X.X)"
git pull --rebase origin main   # 先拉取防冲突
git push origin main
```

- 遇到冲突时，用 `git pull --rebase` 而非 `git pull`，保持提交历史线性
- `package.v2.json` 冲突需手动合并 history 对象，保留所有版本的 changelog

## 插件开发规则

### 语法检查（强制）

修改 `__init__.py` 后、推送前必须执行：

```bash
python3 -m py_compile plugins.v2/mediaservermsgai/__init__.py
```

### 配置页（get_form）

- 使用 Vuetify 组件树 + `types_options` 本地列表
- 双栏布局：左栏「基本设置」+「入库设置」，右栏「过滤设置」+「显示设置」
- 所有控件加 `density='compact'`, `hide-details='auto'` 压缩留白
- 修改时永远用 `patch` 做小范围替换，不要用 `execute_code` 的正则/字符串操作修改 `get_form` 代码——该方法的嵌套字典结构极其复杂，字符串操作极易破坏括号配对

### 仪表盘（get_page）

- 当前已完全移除（`return []`），勿重建
- 如需添加简化状态显示，一律用纯文本 `div`，不要用 `VChip` 标签

### 登录事件（user.authenticated / user.authenticationfailed）

- user.authenticated：登录成功
- user.authenticationfailed：登录失败
- **这两个必须在配置表单中作为独立选项，不能合并**
- 通知排版规则：保留标签+设备信息细分+IP提取

### 深度删除消息

媒体名称限制 120 字符，路径限制 300 字符，挂载路径单条 200 字符，最多显示 5 条。

## 历史经验（避免踩坑）

1. **禁止用 `execute_code` 配合 `re.sub` 修改 `get_form` 或 `get_page` 方法**——嵌套字典结构用字符串替换必出括号不匹配问题
2. **修改后必须先 `py_compile` 再推送**，不可跳过
3. `patch` 比 `execute_code` 安全得多：一次只改一小块，且每次自动跑 lint 检查
4. 双栏布局的 VRow/VCol 嵌套层级很深，替换时务必用 `patch` 匹配足够的上下文行确保唯一性
5. 推送被拒绝时先 `git pull --rebase`，不要 `--force push`（除非用户明确授权）
6. `package.v2.json` 冲突时必须手动合并 history 对象
