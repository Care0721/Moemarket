# Moemarket

**AstrBot 萌宅社区角色卡市场监听与推送插件**

## 简介

Moemarket 是一个运行于AstrBot用于监听 **萌宅社区** 角色卡市场的新卡动态，并将新角色卡的信息（名称、作者、分类、简介、导入数及角色图片）自动推送到指定会话。插件支持分类过滤、轮询间隔自定义、图片优先策略以及推送文案模板，适合希望及时获取萌宅社区最新角色卡资源的用户。

## 功能特性

- 🔄 **自动轮询**：按设定的间隔轮询萌宅社区角色卡市场，无需手动刷新
- 🖼️ **图片推送**：支持推送角色头像或背景图，可配置优先策略
- 🎯 **分类过滤**：只推送指定分类（助手 / 恋爱 / 二次元 / 搞笑 / 故事 / 学习 / 游戏 / 其他），留空则推送全部
- 📢 **多目标推送**：可同时推送到多个群聊或私聊会话
- 📝 **自定义模板**：推送文案模板可自由编辑，字段包括名称、作者、分类、简介、导入数
- 🔐 **登录态管理**：自动处理 Token 获取与过期刷新，登录凭据持久化保存
- 🚫 **去重机制**：已推送过的角色卡不会重复推送，最多保留 5000 条去重记录
- ⚙️ **签名参数自填**：签名密钥、X-App-Pkg、X-App-Ver 等敏感参数由用户自行提供，插件本身不内置任何默认值

## 安装

### 前置条件

- 已部署并可正常运行 AstrBot
- Python 环境（AstrBot 自带）
- 依赖：`aiohttp>=3.9.0`

### 安装步骤

1. **下载插件**

   将本仓库克隆或下载到 AstrBot 的插件目录：

   ```bash
   cd /path/to/astrbot/data/plugins
   git clone https://github.com/Care0721/Moemarket.git
   ```

2. **安装依赖**

   进入插件目录安装 `requirements.txt`：

   ```bash
   cd Moemarket
   pip install -r requirements.txt
   ```

3. **重启或重载 AstrBot**

   在 AstrBot 管理面板中重载插件，或重启 AstrBot 使插件生效。

4. **配置插件**（见下方「配置说明」）后启用监听。

## 配置说明

插件所有配置项均在 AstrBot 的 WebUI 插件配置页面中填写，**无硬编码默认值**，必须完整填写才能正常启动。

| 配置项 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `account` | string | ✅ | 萌宅账号（用户名或邮箱） |
| `password` | string | ✅ | 萌宅密码 |
| `sign_secret` | string (secret) | ✅ | 签名密钥 `signSecret`，需自行获取 |
| `app_pkg` | string | ✅ | 请求头 `X-App-Pkg`，需自行获取 |
| `app_ver` | string | ✅ | 请求头 `X-App-Ver`，需自行获取 |
| `api_base` | string | ✅ | API 地址，例如 `https://cn-api.mengzhai.club` |
| `enable_on_start` | bool | ❌ | 启动时自动开启监听，默认 `true` |
| `poll_interval` | int | ❌ | 轮询间隔（秒），默认 `300`，建议 `180~600` |
| `push_targets` | list | ✅ | 推送目标会话（在 WebUI 中选择群或私聊） |
| `max_new_per_push` | int | ❌ | 单次最多推送新卡数量，默认 `5` |
| `categories` | list | ❌ | 只推送指定分类，留空推送全部 |
| `push_image` | bool | ❌ | 是否推送角色图片，默认 `true` |
| `prefer_avatar` | bool | ❌ | 优先使用头像（否则优先背景图），默认 `true` |
| `message_template` | text | ❌ | 推送文案模板，支持 `{name}` `{author}` `{category}` `{summary}` `{imports}` |

> ⚠️ **签名参数说明**
> `sign_secret`、`app_pkg`、`app_ver` 是访问萌宅社区 API 所需的鉴权参数，插件不提供也不猜测这些值。你需要通过合法途径自行获取并填入，否则插件无法完成登录和请求。

## 使用方法

配置完成后，插件会在 AstrBot 启动时自动开始监听（若 `enable_on_start` 为 `true`）。你也可以通过 AstrBot 的插件管理命令手动启用或停用监听。

当检测到新角色卡时，插件会按照 `message_template` 渲染推送文案，并附带角色图片（若 `push_image` 为 `true`）发送到 `push_targets` 中配置的会话。推送示例：

```
萌宅新角色卡
名称：星野梦
作者：某创作者
分类：二次元
简介：一位来自异世界的魔法少女……
导入数：1.2k
```

## 注意事项

- 插件需要**稳定的网络环境**访问萌宅社区 API，请确保 `api_base` 可达。
- 轮询间隔不宜过短，建议 `180~600` 秒，避免对目标服务造成不必要的压力。
- 登录凭据与 Token 保存在 `data/plugin_data/mengzhai_market/` 目录下，请妥善保管该目录。
- 本插件仅供个人学习与交流使用，请遵守萌宅社区的相关服务条款。