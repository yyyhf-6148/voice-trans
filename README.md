# 实时音频翻译悬浮窗

[![CI](https://github.com/yyyhf-6148/voice-trans/actions/workflows/ci.yml/badge.svg)](https://github.com/yyyhf-6148/voice-trans/actions/workflows/ci.yml)
[![Release](https://github.com/yyyhf-6148/voice-trans/actions/workflows/release.yml/badge.svg)](https://github.com/yyyhf-6148/voice-trans/actions/workflows/release.yml)

抓取**任意指定进程**（或整路系统）的音频，实时翻译为中文，以可调透明度的悬浮窗显示。

**只抓一个进程**是这个项目的核心设计：游戏音效、音乐、Discord 的声音都不会混进来。
默认目标是 EVE Online 舰队语音（Mumble），但**任何应用都能用** —— 改 `audio.process_name` 即可。

[**下载最新版 exe**](https://github.com/yyyhf-6148/voice-trans/releases/latest) · 单文件，目标机无需 Python

---

## 当前进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| **P1** | 音频采集链路（WASAPI loopback → 16k mono） | ✅ 已交付 |
| **P2** | 语音识别（本地 / 云端 / 端到端翻译三条链路） | ✅ 已交付 |
| **P3** | 悬浮窗 UI + 全局热键 + 系统托盘（设置界面）+ 滚轮翻历史 | ✅ 已交付 |
| **打包** | 单文件 exe（目标机无需 Python）+ 首启引导填 Key + 密钥泄漏自检 | ✅ 已交付 |
| **P4** | 进程级捕获（只抓 Mumble，不掺游戏音效） | ✅ 已交付 |
| **P5** | 说话人分离（区分"谁在说"） | ⬜ 待开始 |

---

## 核心方案：Qwen3.5-LiveTranslate

**音频直接进、中文文本直接出**，一次调用完成识别 + 翻译，自带服务端 VAD。

实测数据（16.7s 英文舰队语音）：

| 指标 | 结果 |
|---|---|
| 句尾 → 译文延迟 | **中位 141ms**（目标 <2000ms） |
| 建连耗时 | 1.6s（一次性） |
| 翻译正确率 | 9/9 |

**相对"ASR + 独立翻译"级联方案的核心优势：术语热词表。**
`gate` 能被强制译成"星门"而不是"大门"，这在舰队指挥里是致命的差别。
内置热词表 **629 条**（`terms.yaml`，由 EVE SDE 自动生成），覆盖舰船 / 模块 / 无人机 / 战术术语。

三条链路可选（`asr.provider`）：

| provider | 说明 |
|---|---|
| `livetranslate` | **默认**。阿里云百炼，端到端，自带服务端 VAD，支持热词表 |
| `cloud` | 云端 ASR（OpenAI 兼容）+ 独立翻译步骤 |
| `local` | 本地 faster-whisper。⚠️ 见下方"已知限制" |

---

## 环境要求

**只用打包好的 exe**：Windows 10/11 + 阿里云百炼 API Key，就这些。不需要 Python。

**从源码跑**：

- Windows 10 19041+（进程级捕获需要；全局 loopback 无此限制）
- Python 3.12（**不要用 3.14**，科学计算包兼容性有风险）
- 阿里云百炼 API Key（[获取地址](https://bailian.console.aliyun.com/)）

---

## 快速开始

> **只想自己用、不想碰命令行？** 跳到 [打包成单文件 exe](#打包成单文件-exe发给别人用) ——
> 双击 `build_exe.bat` 生成一个 exe，拷到哪台机器都能直接双击运行。

### 1. 安装依赖

双击 `setup.bat`，或手动：

```bat
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 2. 配置 API Key

复制 `.env.example` 为 `.env`，填入：

```
DASHSCOPE_API_KEY=sk-你的百炼Key
```

**不要把 Key 写进 config.yaml。** `.env` 已加入 `.gitignore`。

> 也可以走系统环境变量（`.env` 中已有的值不会被覆盖）：
> ```bat
> setx DASHSCOPE_API_KEY "sk-你的key"
> ```

### 3. 启动

双击 **`run.bat`**，或：

```bat
.venv\Scripts\python.exe -m src.app
```

首次启动约 2-3 秒（建连 + 音频子系统预热）。悬浮窗出现在右下角。

### 4. 命令行验证（先确认链路通）

```bat
REM 抓系统音频，实时翻译 60 秒（能看到原文 + 译文 + 延迟）
.venv\Scripts\python.exe -m src.tools.translate_live -d 60

REM 无声卡环境：灌 WAV 文件做确定性验证（2 倍速播放）
.venv\Scripts\python.exe -m src.tools.translate_live --wav _en_sample.wav --speed 2
```

---

## 打包成单文件 exe（发给别人用）

```bat
build_exe.bat
```

产物 `dist\EVE-Voice-Translator.exe` —— **单文件 94.9 MB，双击即用，目标机不需要装 Python。**

### 首次使用：填入 API Key 就能用

1. 把 exe 拷到任意目录（桌面就行），双击
2. 程序发现没有 Key，**不白建连**，直接把「设置窗口」推到面前
3. 在「接口与密钥 → API Key」里粘贴 Key，点「保存并应用」
4. 提示需要重启 → 点「是」，重启后开始翻译

**不需要手工建 `.env`，也不需要改 `config.yaml`。**
Key 由设置窗口写进 **exe 同目录**的 `.env`。

### API Key 绝不进包（三重保障）

| 环节 | 手段 |
|---|---|
| 打包前 | `trans.spec` 的 `_assert_no_env_bundled()`：datas 里一旦出现 `.env` / `api_keys.json` / `secrets.yaml` 就**直接中止构建** |
| 打包后 | `verify_bundle.py`：`CArchiveReader` **按文件名**拦截 → 解压 **2456 个条目**搜密钥真实值 → 通用 `sk-` 形态扫描（**不依赖 `.env` 存在**，能兜住"硬编码进代码"） |
| 运行期 | Key 只写 exe 同目录的 `.env`；`config.yaml` 里只有环境变量**名**，没有值 |

> ⚠️ **不能**用 `b"sk-xxx" in open(exe, "rb").read()` 这种方式检查。
> PyInstaller 归档是 zlib 压缩的，明文在 exe 里搜不到任何东西 ——
> 这种写法即使 Key 真被打进去也会**假通过**，比不检查更危险。

### 打包后必跑的两步

```bat
REM 1. 密钥 / 内置资源 / 体积自检
.venv\Scripts\python.exe -m src.tools.verify_bundle dist\EVE-Voice-Translator.exe

REM 2. 在干净目录里真跑一遍 exe（三阶段）
.venv\Scripts\python.exe -m src.tools.smoke_test_exe dist\EVE-Voice-Translator.exe
.venv\Scripts\python.exe -m src.tools.smoke_test_exe dist\EVE-Voice-Translator.exe --online
```

`smoke_test_exe` 专门覆盖**源码测试一个都覆盖不到**的打包坑：

| 阶段 | 验什么 |
|---|---|
| A 无 Key | 退出码 2 + exe 旁边自愈出 `config.yaml` + **`logs/app.log` 真的被创建** |
| B 假 Key | 退出码 4 且日志走到「启动失败」→ 证明 `soundcard` / `dashscope` 在冻结环境里 import 成功 |
| C 真 Key（`--online`） | 退出码 0 + 日志出现「服务已就绪」 |
| D 窗口模式（`--online`） | 退出码 0 + 日志出现「进入事件循环」「托盘图标已显示」→ 证明 **Qt 平台插件在冻结环境里能加载** |
| E 重启自身（无需 Key） | 父进程真的 `Popen` 一次自己再退出，子进程必须活下来 → 证明 **onefile 重启不崩** |

> A/B/C 全走 `--no-overlay`，而那条路**根本不 import PySide6** ——
> 也就是说「冻结环境里 Qt 平台插件能不能加载」（PyInstaller + PySide6 的经典翻车点）
> 完全没被覆盖，偏偏用户双击 exe 走的正是这条路。所以阶段 D 不加 `--no-overlay`，
> 靠 `--duration` 让它跑 5 秒后自动退出（屏幕上会短暂闪一下悬浮窗）。

> 阶段 E 走的是 `App._run_selftest_restart()`（内部开关 `--selftest-restart`，
> 不在 `--help` 里露出）。**这条路径 A~D 一个都不走**，而用户实机报的崩溃恰恰出在这里：
> 见下方「重启自身为什么会崩」。

### 重启自身为什么会崩（改 `_restart()` 前必读）

PyInstaller onefile 的进程模型是**三层**：

```
外层引导器  ──解压归档到 %TEMP%\_MEIxxxxxx，设好环境变量后──▶  子进程（真正跑你的代码）
                                                              │
                                    应用自己 Popen(sys.executable) ─▶ 孙进程
```

引导器给子进程设的环境变量里有 `_MEIPASS` 和 `_PYI_PARENT_PROCESS_LEVEL=1`，
**子进程看到它们就知道"包已经解好了，别再解一遍"**。

于是：应用（子进程）如果直接 `Popen(sys.executable)` 而不清理环境变量，
孙进程会**继承**这两个变量 → 复用同一个 `_MEIxxxxxx`。
可应用一退出，**外层引导器就会把 `_MEIxxxxxx` 整个删掉** ——
孙进程正边被删边 import（`PySide6.QtWidgets` 有上百个文件），于是：

```
ModuleNotFoundError: No module named 'PySide6.QtWidgets'
```

**修复**：`src/config.py::child_process_env()` 在重启前剥掉所有
`_MEI*` / `_PYI_*` 前缀的环境变量，让新进程**重新解压自己的 `_MEI` 目录**，
与父进程彻底解耦。`_restart()` 必须传 `env=child_process_env()`。

> 这个 bug 只在打包产物里出现，源码运行永远复现不了（源码模式没有引导器、没有 `_MEI`）。
> 阶段 E 会断言父子进程的 `_MEIPASS` **不相同** —— 只要有人把 `env=` 去掉，测试立刻红。

> 阶段 A 那条"日志文件真的被创建"不是凑数：`logging_setup._CONFIGURED`
> 的幂等提前返回曾让**文件 handler 永远装不上**（模块级 `get_bound_logger()`
> 在 import 时就吃掉了一次无参初始化）。源码模式有控制台输出所以完全看不出来，
> 只有真跑 windowed exe 才暴露 —— 表现是"哪儿都没有日志"。

### 路径规则（改 `config.py` 前必读）

| | 源码运行 | 打包运行 |
|---|---|---|
| `PROJECT_ROOT` | 项目根 | **exe 所在目录** |
| `config.yaml` / `.env` / `logs/` | 项目根 | exe 同目录 |
| 内置只读资源 | 项目根 | `sys._MEIPASS`（**进程退出即删**） |

⚠️ **绝不能**把 `.env` 写进 `sys._MEIPASS` —— 那是 onefile 的临时解包目录，
关一次程序就没了，用户填好的 Key 会凭空消失。
`ensure_user_files()` 只把内置 `config.yaml` 铺到 exe 旁边，且**已存在绝不覆盖**。
exe 目录不可写时 `log_dir` 兜底到 `%LOCALAPPDATA%\EVEVoiceTranslator`。

### 94.9 MB 是怎么来的

`trans.spec` 排除了运行期**一次都不 import** 的重型依赖：
`torch` / `torchaudio` / `faster-whisper` / `ctranslate2` / `transformers` /
`numba` / `pandas` / `matplotlib` / `tkinter` / `PyQt5` / `PyQt6`，
以及 PySide6 里没用到的 `QtWebEngine*` / `QtQuick*` / `QtMultimedia` / `QtCharts` /
`Qt3D*` / `QtBluetooth` / `QtSql` / `QtTest` / `QtPdf` 等。

**不排除的话体积会从 ~95 MB 涨到 3 GB+。** 本项目固定 `asr.provider = livetranslate`，
本地 whisper 那条链路运行期不会被执行，排除是安全的。

`upx=False` —— UPX 能再压小一些，但会显著抬高杀软误报率，不划算。

---

## 全局热键

| 热键 | 功能 |
|---|---|
| `Ctrl+Alt+H` | 显示/隐藏悬浮窗 |
| `Ctrl+Alt+↑` | 提高不透明度（每次 +0.05） |
| `Ctrl+Alt+↓` | 降低不透明度 |
| `Ctrl+Alt+M` | 切换点击穿透（不挡游戏操作） |
| `Ctrl+Alt+R` | 悬浮窗回到配置位置 |
| `Ctrl+Alt+Q` | 退出 |

免管理员权限，且**不会吃掉按键**（游戏照常收到这些组合键）。

---

## 系统托盘

托盘图标是**鼠标可达的操作入口**（悬浮窗无边框、没有关闭按钮）。

| 菜单项 | 说明 |
|---|---|
| 显示字幕 | 同 `Ctrl+Alt+H`，勾选状态与窗口同步 |
| 点击穿透 | 同 `Ctrl+Alt+M` |
| 清空字幕 | 清掉屏幕上的历史 |
| **设置…** | 打开设置窗口（见下） |
| 重新连接 | 重建云端 WebSocket（网络抖动后用） |
| 打开配置文件 / 打开日志目录 | 直接用资源管理器打开 |
| 重启程序 | 保存后自动重启（改完需重启的设置用） |
| 退出 | 同 `Ctrl+Alt+Q` |

左键单击图标 = 显示 / 隐藏字幕。图标颜色反映状态：
<span>蓝=正常</span> / <span>红=出错</span> / <span>灰=连接中</span>。

用 `tray.enabled: false` 或 `--no-tray` 可关闭托盘。

### 设置窗口

**五个分页，覆盖「本程序实际用到」的全部 55 个配置项**。
`cloud` / `local` 后端、级联翻译、本地 VAD 这些在 livetranslate 模式下
**运行期不被读取**的项已从界面收起（仍保留在 `config.yaml`，需要时手动改）。
改完点「保存并应用」：

| 分页 | 可改内容 |
|---|---|
| **接口与密钥** | ①**接口与模型**：模型名、**接口地址（WebSocket 端点）**、业务空间 ID、Key 环境变量名、**API Key**（只写 `.env`）；②**翻译参数**：启用开关、源/目标语言、双语开关、判句静音时长、服务端 VAD 灵敏度、音色、连接超时、**热词表文件（`terms.yaml`）+ 内联热词表** |
| **音频** | 采集后端、目标进程名、进程采集缓冲、采集设备（实时枚举）、采集分块、队列上限、自动增益、静音阈值 |
| **字幕** | 开关、不透明度、位置、宽高、字号、字体族、行距、边距、保留时长、自动淡出、条数上限、单条字符上限、自动滚动、显示原文、点击穿透、5 个配色、说话人配色 |
| **快捷键与托盘** | 6 个全局热键、托盘开关 |
| **高级** | 日志级别、日志目录、内部采样率（**只读**，改它整条链路都错） |

**连接信息直接显示，不用猜**（「接口与密钥」页第一组）：

- **接口地址**（`asr.livetranslate.url`）：配置为 `null` 时框里**直接显示**
  实际生效的官方通用端点 `wss://dashscope.aliyuncs.com/api-ws/v1/realtime`，
  而不是留个空框。要用业务空间专属域名时才改成别的，并补上 `workspace`。
  > 只显示 ≠ 改动：没动过这个框，保存时**不会**把默认值写回 `config.yaml`
  > （`_read()` 会把它还原成 `None`），所以不会产生配置漂移。
- **API Key**：与接口地址**同组相邻**，**明文预填**当前已配置的密钥，
  打开设置就能一眼看到连到哪里、用的什么 Key。
  只有**真的改过**才写回 `.env`（预填不算改动，留空 = 不修改）。
  > ⚠️ 密钥明文显示在窗口里，**录屏 / 直播时注意遮挡**。

**保存前会校验**（红字提示，拦住非法值）：

- `asr.livetranslate.url` 必须 `ws(s)://` 开头（**留空允许**，表示用官方端点）
- `asr.livetranslate.api_key_env` 必须是合法环境变量名

**热生效规则：**

| 改动 | 生效方式 |
|---|---|
| `overlay.*`（透明度 / 字号 / 配色 / 位置 / 条数…） | **立即生效**，窗口不闪 |
| `hotkeys.*` | **立即生效**（自动注销旧热键再注册，不会重复） |
| `asr.*` / `audio.*` / `app.*` / `tray.*` / API Key | 提示**重启**，可一键重启 |

**API Key 只写 `.env`**（该文件已在 `.gitignore` 里）。
设置框里**明文预填**当前密钥，与接口地址同组相邻；**留空表示不修改**，
绝不会把密钥写进 `config.yaml`。只有真的改过才写 `.env`。
环境变量的**名字**（`asr.*.api_key_env`）是普通配置，照常写 `config.yaml`。

> 设置窗口是**行级手术**写入 `config.yaml` —— 只替换目标键的值，
> 文件里的注释、缩进、顺序全部原样保留，不会因为一次设置丢失项目注释。
>
> 「界面不退化」这件事由 `tests/test_settings_coverage.py` 守着：
> 它递归枚举 `Config` 的全部 **89** 个叶子键，断言每个键要么有界面字段（55 个）、
> 要么在 `_EXCLUDED` 里写明理由（34 个），然后**逐个字段做真实扰动 → 写入 → 重载**验证。
> 以后加配置项忘了加界面，测试会直接红。

---

## 配置说明（config.yaml）

### 采集

| 配置项 | 默认 | 说明 |
|---|---|---|
| `audio.backend` | `process` | `process` = 只抓指定进程（Mumble 专属）；`wasapi_loopback` = 抓整路系统音频 |
| `audio.process_name` | `mumble` | 目标进程名（不含 `.exe`），仅 `backend=process` 时生效。**精确匹配优先**，避免误命中 `mumble-g15-helper.exe` |
| `audio.process_buffer_ms` | 50 | 进程级 WASAPI 缓冲，仅 `backend=process` 时生效。越小延迟越低，不建议低于 20 |
| `audio.device_keyword` | 空 | 仅 `backend=wasapi_loopback` 时生效。留空 = 默认扬声器 |
| `audio.block_ms` | 32 | 采集块大小，越小延迟越低 |
| `audio.auto_gain` | `true` | 峰值归一化，提升识别准确率 |

> 找不到目标进程时**自动降级**为整路 loopback 并写告警日志，程序不会因此启动失败。
> 也可用环境变量临时覆盖：`AUDIO_BACKEND`、`AUDIO_PROCESS_NAME`。

### 识别 / 翻译

| 配置项 | 默认 | 说明 |
|---|---|---|
| `asr.provider` | `livetranslate` | 见上方"核心方案" |
| `asr.livetranslate.target_language` | `zh` | 目标语言 |
| `asr.livetranslate.silence_duration_ms` | 500 | 服务端 VAD 判句静音时长 |
| `asr.livetranslate.phrases_file` | `terms.yaml` | **外部热词表文件**（YAML 顶层 `术语: 译法`，629 条）。相对路径按 `config.yaml` 所在目录解析 |
| `asr.livetranslate.phrases` | `{}` | **内联热词表**（少量临时覆盖/追加）。与外部表合并时**内联优先** |
| `asr.livetranslate.with_original` | `true` | 是否同时返回英文原文（双语字幕） |
| `vad.enabled` | `false` | ⚠️ 用 livetranslate 时**必须为 false** |

> **热词表 = 外部文件 `terms.yaml` + `config.yaml` 内联，同名以内联为准**；合并后总数上限 **1000** 条，超出启动报错。
> `terms.yaml` 由 `src/tools/build_phrases.py` 从 EVE SDE `types.jsonl` 自动生成（分区：手写战术术语 50 / 舰船 410 /
> 建筑结构 21 / 无人机 78 / 模块基名 46 / 口语短名 24）。**热词表是建连时一次性下发**，改完必须重启程序或点托盘「重新连接」。
> 设置界面的「热词表文件」栏会实时显示「外部 N 条 + 内联 M 条 → 合并 K 条 / 上限 1000」。

> `asr.provider=livetranslate` 与 `vad.enabled=true` 冲突时，**启动会直接报错并给出修正提示**。
> 该模型自带服务端 VAD，本地再切一次会把句子切碎。

### 悬浮窗

| 配置项 | 默认 | 说明 |
|---|---|---|
| `overlay.opacity` | 0.85 | 不透明度 |
| `overlay.position` | `bottom_right` | 位置（五选一） |
| `overlay.click_through` | `false` | 启动时点击穿透 |
| `overlay.hold_seconds` | 30 | 字幕保留时长（0 = 只按条数淘汰） |
| `overlay.max_messages` | 30 | 最多保留条数 |
| `overlay.background` | `#E6101216` | 背景色，支持 `#AARRGGBB` 自带透明度 |
| `overlay.color_translation` | `#FFFFFF` | 译文颜色 |
| `overlay.color_original` | `#9AA4B2` | 原文颜色 |
| `overlay.auto_fade_after_s` | 0 | 静置多久后淡出（0 = 不淡出） |
| `overlay.auto_scroll` | `true` | 新条目自动滚到底部（详见下方「翻看历史」） |

#### 翻看历史（滚轮）

鼠标**滚轮上滚**即可翻看之前被打出来的字幕：

| 行为 | 说明 |
|---|---|
| 上滚翻历史 | 自动**暂停跟随**新消息 —— 新字幕照常入库，但视口不动 |
| 右下角提示 | 视口下方还有内容时，右下角显示「▼ 下方 N 条」药丸 |
| 滚回底部 | 自动**恢复跟随**，新消息继续往下走 |
| 翻看时阅读位置 | 用**条目锚点**而非像素偏移定位，新消息在尾部增长、旧条目被 `max_messages` 从头部裁掉，阅读位置都不会跑 |
| `auto_scroll=false` | 首次出现溢出就冻结视口，此后新消息完全不动视口，只能手动滚 |

> ⚠️ **`click_through: true` 时滚轮不生效** —— 鼠标事件整体穿透到游戏了。
> 想翻历史先按 `Ctrl+Alt+M` 临时关掉穿透，翻完再按回来。
>
> 内容没超出 `max_height` 时（无溢出），滚轮会被忽略，窗口仍底部对齐。

### 托盘

| 配置项 | 默认 | 说明 |
|---|---|---|
| `tray.enabled` | `true` | 是否显示托盘图标 |

> 上面这些项都可以在**设置窗口**里改，不必手编 `config.yaml`。

---

## 调试与验证工具

| 命令 | 用途 |
|---|---|
| `python -m src.tools.list_devices` | 列出音频设备，查 `device_keyword` 该填什么 |
| `python -m src.tools.audio_monitor -d 20` | 监测采集电平 + 录制 wav |
| `python -m src.tools.translate_live --show-vad` | 显示服务端 VAD 的说话起止（排查切句问题） |
| `python -m src.app --no-overlay` | 无窗口运行（只看日志） |
| `python -m src.app --no-tray` | 不显示托盘图标 |
| `python -m src.app --duration 5` | 带窗口跑 5 秒后自动退出（验 Qt 能不能起来） |
| `python -m src.app --selftest restart` | 内部自检：真跑一遍「重启自身」（打包后最易崩的一条路） |
| `python -m src.app --selftest settings` | 内部自检：构造一次设置窗口（首启引导的必经之路） |
| `python tests/preview_overlay_live.py` | 悬浮窗实机截图（**调样式用这个**） |
| `python tests/preview_overlay_live.py --scroll` | 滚动演示连拍三张（跟随尾部 / 上滚翻历史 / 回到底部） |
| `python tests/preview_settings_live.py` | 设置窗口 5 个分页实机截图 |
| `python -m src.tools.verify_bundle dist\*.exe` | 打包产物自检：密钥泄漏 / 内置资源 / 体积 |
| `python -m src.tools.smoke_test_exe dist\*.exe` | 干净目录里真跑 exe（A~F 六阶段；`--online` 连真服务） |
| `python -m src.tools.make_app_icon` | 重新生成 `assets/app.ico` |

### 测试套件

```bat
.venv\Scripts\python.exe tests\test_core.py                        REM 87 项
.venv\Scripts\python.exe tests\test_livetranslate.py --offline     REM 26 项（无需 Key）
.venv\Scripts\python.exe tests\test_overlay.py                     REM 167 项
.venv\Scripts\python.exe tests\test_e2e_overlay.py                 REM 12 项（端到端，需 Key）
.venv\Scripts\python.exe tests\test_config_writer.py               REM 56 项（配置写回）
.venv\Scripts\python.exe tests\test_tray_settings.py               REM 87 项（托盘 + 设置）
.venv\Scripts\python.exe tests\test_settings_coverage.py           REM 36 项（设置项覆盖率）
.venv\Scripts\python.exe tests\test_process_loopback.py            REM 47 项（加 --capture 再 +6）
```

源码合计 **518 项**，全部通过（`test_process_loopback.py` 加 `--capture`
再启用 6 项真机采集，共 524 项）。
`test_overlay.py` / `test_tray_settings.py` / `test_settings_coverage.py`
会自动切到 `QT_QPA_PLATFORM=offscreen`，无需桌面环境。

打包产物另有 **39 项** 冒烟断言（`src/tools/smoke_test_exe.py`，阶段 A~F；
不加 `--online` 时跑 **28 项**）+ **4 项** 密钥/资源自检（`src/tools/verify_bundle.py`）。

---

## 项目结构

```
trans/
├─ config.yaml                   # 主配置
├─ .env                          # API Key（不入库）
├─ main.py                       # ★ 打包入口（薄壳，只负责把 src.app 拉起来）
├─ trans.spec                    # ★ PyInstaller 配置（datas / excludes / 密钥拦截）
├─ build_exe.bat                 # ★ 一键打包（图标 → 构建 → 自检）
├─ run.bat                       # 启动（双击即可）
├─ setup.bat                     # 安装依赖
├─ assets/
│  └─ app.ico                    # 程序图标（由 make_app_icon 程序化生成）
├─ src/
│  ├─ app.py                     # 顶层装配：配置→日志→预热→窗口→托盘→流水线→热键
│  ├─ config.py                  # 配置加载 + 严格校验 + 打包路径解析（fail fast）
│  ├─ config_writer.py           # ★ 配置写回（行级手术，保留注释）
│  ├─ pipeline.py                # 流水线（切句-识别 / 流式直推 两种模式）
│  ├─ errors.py                  # 类型化异常层级
│  ├─ logging_setup.py           # 结构化日志（控制台彩色 + 文件轮转）
│  ├─ audio/
│  │  ├─ device.py               # 设备枚举与自动选择
│  │  ├─ ring_buffer.py          # 环形缓冲 + 重采样
│  │  ├─ process_loopback.py     # ★ 进程级 loopback（纯 ctypes WASAPI）
│  │  └─ capture.py              # 采集调度（进程级 / 整路 loopback 双后端）
│  ├─ asr/
│  │  ├─ base.py                 # AsrProvider 接口
│  │  ├─ factory.py              # Provider 工厂
│  │  ├─ live_translate.py       # ★ Qwen3.5-LiveTranslate（流式）
│  │  ├─ cloud_whisper.py        # 云端 ASR（OpenAI 兼容）
│  │  ├─ local_whisper.py        # 本地 faster-whisper
│  │  └─ vad.py                  # Silero VAD 切句
│  ├─ ui/
│  │  ├─ subtitle_model.py       # 字幕模型（纯逻辑，可脱离 Qt 单测）
│  │  ├─ overlay.py              # 透明置顶悬浮窗
│  │  ├─ hotkeys.py              # 全局热键
│  │  ├─ tray.py                 # ★ 系统托盘图标 + 菜单
│  │  └─ settings_dialog.py      # ★ 设置窗口（schema 驱动，聚焦 livetranslate）
│  └─ tools/
│     ├─ translate_live.py       # ★ 实时翻译命令行验收
│     ├─ transcribe_live.py      # 切句-识别模式验收
│     ├─ audio_monitor.py        # 采集电平监测
│     ├─ list_devices.py         # 设备枚举
│     ├─ make_app_icon.py        # ★ 生成 assets/app.ico
│     ├─ verify_bundle.py        # ★ 打包产物密钥/资源/体积自检
│     └─ smoke_test_exe.py       # ★ 干净目录里真跑 exe（三阶段）
├─ tests/                        # 518 项自检（test_process_loopback 加 --capture 再 +6）
```

---

## 已知限制

### 音频来源
- **默认只抓 Mumble 单进程**（`audio.backend=process`，WASAPI ApplicationLoopback）。
  实测隔离度 ≈ 14000 倍（-83 dB）—— 只放音调进程时，Mumble 侧 RMS 仅 1.5e-05。
- 进程级捕获需要 **Windows 10 2004（Build 19041）及以上**；旧系统会自动降级为整路 loopback。
- 若 `backend=wasapi_loopback`，则**会抓到所有系统声音**（EVE 音效、音乐、Discord 等）。
- **Mumble 若使用 exclusive mode**，进程级 loopback 仍可捕获（它不走设备混音）；
  shared mode 亦实测可抓。
- **说话人分离不在本阶段范围**：Mumble 输出是混音，本地无法从混音里拆出独立人声。
  只能靠 pyannote 之类的音色聚类区分"是不是同一人"（P5）。

### 本地 GPU 方案（`asr.provider=local`）在本机不可用
N 卡 **P106-100 空闲 3-5 秒后会从 P0/1506MHz 掉到 P8/607MHz**，
唤醒开销 ~570-600ms，识别延迟从 ~320ms 劣化到 ~900-1100ms。
舰队语音间隔恰好 2-5 秒，每次都撞上这个开销。

**软件保活/热身/`nvidia-smi -lgc` 锁频全部实测无效**（驱动限制）。
故 ASR 主力为云端，本地方案保留为备选。

### 热词表的边界
热词表匹配的是**服务端 ASR 输出的字面文本**，ASR 拼错时热词表就失效。
实测遇到过：
- `Logie`（应为 `Logi`）→ 未命中热词表
- `Sino`（应为 `Cyno`）→ 未命中热词表

**建议把常见拼写变体直接追加到 `terms.yaml`**（`logie`、`sino`、`neuting` 等）。
匹配**不区分大小写**（2026-09-28 联机 A/B 实测：ASR 输出小写 `gate`，大写键 `Gate` 同样命中），
所以键**统一小写**即可，不需要大小写双写。

> 词表很大时**别塞进 `config.yaml`**：设置界面的内联框是按「几十条」设计的，
> 一次渲染 629 行既卡又容易误改。大表写进 `terms.yaml`，内联框只放临时覆盖。

### 说话人分离未实现
Mumble 输出是**混音**，本地无法从混音里拆出独立人声（数学上不可逆）。
只能靠 pyannote 等模型做音色聚类（不要求知道"是谁"，只区分"是不是同一人"）。
计划在 P4 集成。

---

## 协议要点（改字幕相关代码前必读）

### 原文/译文的到达顺序：原文在前

服务端的事件顺序是**原文定稿在前、译文定稿在后**，两者**序号相差 1**
（pipeline 对每个事件都自增序号，所以序号不能用来跨事件配对）：

```
original    seq= 3  en='All right. Fleet.'
translation seq= 4  zh='好的。舰队。'
```

`SubtitleStore` 因此用"**挂起 + 认领**"：原文先到时先挂起
（`_pending_original`），紧随其后的译文条目创建时把它认领过来，合成一条双语字幕。
同时保留"译文先到、原文后到"的路径（同序号直接归属 / 挂到活动条目）。

⚠️ **"认领挂起原文"必须排在"合并到活动条目"之后**：上一句的译文如果还在流式
输出，此时到达的原文其实属于**下一句**，绝不能被上一句的译文认领。

回归由两处守着，改动这里务必两个都跑：
- `tests/test_overlay.py` →「原文先到 / 译文后到（实测服务端顺序）」+
  「双语配对：复刻 app.py 的真实事件路由（离线复现）」（离线，无需 Key）
- `tests/test_e2e_overlay.py` →「悬浮窗配对与事件流完全一致（无错位）」
  （真实 API，按事件流重建期望配对后**逐对全量比对**，而不是只看"有没有配上"）

---

## 故障排查

| 现象 | 处理 |
|---|---|
| 悬浮窗看不到 | 可能被切到穿透且移出屏幕：按 `Ctrl+Alt+H` / `Ctrl+Alt+R` |
| 悬浮窗挡住游戏鼠标 | 按 `Ctrl+Alt+M` 开启点击穿透 |
| 提示配置校验失败 | 按报错提示改。最常见的是手工把 `vad.enabled` 改成了 `true`（livetranslate 必须 `false`） |
| 启动报 API Key 未设置 | 用 exe 时**设置窗口会自动弹出**，粘贴 Key → 保存并应用 → 重启。从源码跑则确认 `.env` 里 `DASHSCOPE_API_KEY` 已填且格式为 `sk-...` |
| 一直没有翻译出来 | 系统没有声音在播放；用 `--show-vad` 确认服务端是否检测到说话 |
| 短句（Yes/Go）没字幕 | 服务端 VAD 需要一定语音长度，极短应答可能不触发 |
| 采集到的声音不对 | 默认扬声器可能不是你在用的，用 `list_devices` 查名字后填 `device_keyword` |
| 术语翻译不准 | 在 `terms.yaml`（或内联 `asr.livetranslate.phrases`）里加热词，含拼写变体 |
| **托盘图标不见了** | Windows 会折叠不活跃的图标，点任务栏的 `^` 展开；确认 `tray.enabled: true` |
| **设置改完没生效** | `overlay.*` / `hotkeys.*` 是立即生效的；其余会弹「需要重启」，点「是」即可 |
| **设置里找不到某个配置项** | 界面只暴露 livetranslate 模式下**实际生效**的 56 个键；`cloud` / `local` / 级联翻译 / 本地 VAD 已收起，需要时直接改 `config.yaml`（`tests/test_settings_coverage.py` 会核对 90 个键的去向） |
| **接口地址填哪** | 「接口与密钥」页第一组的「接口地址（WebSocket 端点）」，**框里已直接显示实际生效的官方端点**，一般不用改 |
| **API Key 在哪看 / 改** | 就在「接口地址」下面一行，**明文显示当前密钥**；改完保存即写入 `.env` |
| **设置保存失败** | 报错会指出是哪个键 —— 通常是 `config.yaml` 被手工改坏了，用 `git diff` 看看 |
| **找不到退出口** | `Ctrl+Alt+Q`，或托盘右键 → 退出 |

### 打包版（exe）专有

| 现象 | 处理 |
|---|---|
| **双击 exe 没反应 / 一闪而过** | 看 exe **同目录**的 `logs\app.log`。若连 `logs` 目录都没有，多半是 exe 放在不可写目录（如 `C:\Program Files`），换个目录即可 |
| **首次打开提示「未配置 API Key」** | 这是**预期行为**。设置窗口会自动弹出，粘贴 Key → 保存并应用 → 点「是」重启 |
| **保存后重启出来的程序报 `ModuleNotFoundError: No module named 'PySide6.QtWidgets'`** | 已修（`_restart()` 未剥掉 PyInstaller 引导器环境变量，导致新进程复用了父进程**即将被删除**的 `_MEI` 目录）。升级到含 `child_process_env()` 的版本即可 |
| **把 exe 发给别人，我的 Key 会一起过去吗** | 不会。Key 存在 exe **同目录**的 `.env`，不在 exe 内部。发出去的 exe 是干净的，对方首次运行会被引导填自己的 Key |
| **别人拿到 exe 后没有 config.yaml** | 不用管。首次运行会从 exe 内置资源自愈出一份到 exe 旁边（已存在则绝不覆盖） |
| **想确认 exe 里真的没有我的 Key** | `python -m src.tools.verify_bundle dist\EVE-Voice-Translator.exe` |
| **exe 启动比源码慢几秒** | 正常。onefile 每次启动都要把 ~95MB 解压到 `%TEMP%`，一次性开销约 2-4 秒 |
| **偶发**：程序已正常退出，却弹出「程序已停止工作」 | 已知问题，频率约 **2%**（实测 44 次窗口模式启动中 1 次）。崩溃码 `0xC000041D`（用户回调异常），发生在**日志已写完「正在关闭」之后**的收尾阶段，**不影响任何功能**（配置、字幕、日志都已落盘）。触发条件尚不明确（疑似 websocket / WASAPI 原生线程与解释器终结竞争） |
