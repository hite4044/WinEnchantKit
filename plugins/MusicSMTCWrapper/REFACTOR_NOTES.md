# MusicSMTCWrapper 重构说明（原位置 → 新位置）

重构目标：把原本集中在 `main.py::Plugin` 的职责拆成「编排 / SMTC 宿主 / 后端模板 / 通用缓存 / 音乐报告」五层，后端采用**模板方法模式**。

```
Plugin (main.py)                     只做流程编排 + 管理各 MusicBackend 子类
  ├─ SMTCHost (smtc_host.py)         自建 SMTC 会话 + 源会话的全部细节
  ├─ MusicBackend (_smtc_backend_base.py, 基类)
  │      get_music_info(props, size) 统一流程:
  │          from_cache(music_info, size)      ← 本地缓存
  │        -> from_web(music_info, size)       ← 联网搜索下载
  │        -> from_raw_smtc(music_info, size)  ← 原 SMTC 低清封面
  │      某一步返回 None 就进入下一步; force_refresh=True 跳过 from_cache
  │      ├─ MusicCache (基类持有, 缓存的加载 / 保存 / 查找器)
  │      └─ KugouMusicBackend (_smtc_backend_kugou.py, 只写酷狗业务)
  └─ MusicReportManager (music_reporter.py)   音乐报告（从 Plugin 剥离）
```

## 为什么实现文件用 `_smtc_backend_*` 私有模块名

WinEnchantKit 的插件加载器 `gui/control_panel.py::load_plugin` 只做 `sys.path.insert(0, plugin_dir)`。
而 `plugins/HDKugouCover/backend.py` 会占用顶层模块名 `backend`，仓库根 `base.py` 占用顶层 `base`；
实测在 HDKugouCover 之后加载本插件时 `import backend.base` 会 `ModuleNotFoundError: 'backend' is not a package`，
`from base import ...` 也会被仓库根 `base.py` 抢先解析。
因此本插件的契约层与后端实现放在**插件根目录**并使用私有前缀 `_smtc_backend_*`，
任何加载顺序下都能唯一解析，也不污染其它插件。

## 文件职责

| 文件 | 职责 |
|---|---|
| `main.py` | `Plugin`：配置、后端管理、源会话生命周期编排、平台选择、封面尺寸策略、缓存按钮、快捷方式 |
| `smtc_host.py` | `SMTCHost`：自建 `MediaPlayer`+SMTC 会话、源会话发现、播放状态、进度条、`display_updater` 写入、按钮转发 |
| `_smtc_backend_base.py` | `StandardMusicInfo`（纯数据）、`MusicCache`（加载/保存/查找）、`MusicBackend`（模板方法基类）、`wait_result`/`make_music_hash`/`sanitize_filename`/`normalize_size`/`plugin_logger` |
| `_smtc_backend_kugou.py` | `KugouMusicBackend`（实现 `from_cache`/`from_web`/`from_raw_smtc`/`encode_record`/`decode_record`）、酷狗搜索与封面工具 |
| `backend\base.py` | 公共定义再导出入口（薄层，不定义类） |
| `backend\base_backend.py` | 旧的通用/酷狗混合模块，现为兼容再导出薄层 |
| `music_reporter.py` | `MusicReportManager`（报告决策与落盘）+ `Music`/`MusicPoint`/`MusicReporter` |

## 三层契约

### StandardMusicInfo（纯数据）
`title / artist / album_title / album_artist / length / cover(PIL.Image | None) / has_cover`。
只是一组歌曲信息的包装：**不用于缓存**，没有 `load`/`dump`。`cover=None` 表示没有可用封面，
`SMTCHost` 会保留源平台原缩略图。

### MusicCache（缓存的加载、保存与查找器，后端持有）
* 只管存储与查找，缓存数据怎么编撰/解读是后端的职责；
* 歌曲信息：`dict[int, dict]`（key = 后端提供的歌曲 hash，value = 后端提供的记录），整体存 `music_info.json`；
* 封面：`{歌曲名} - {作者}_{music_hash}_{尺寸}.{后缀}`，内存索引 `hash -> [(尺寸, 文件名)]` 加速查找；
* 查找规则：优先取**等于或大于**请求尺寸的最小者并缩放到请求尺寸；没有更大的就取最大封面缩放；
  没有符合条件的封面（信息在但封面文件缺失/损坏）则返回 `None` → 自动进入下一步；
* 封面尺寸硬限制 8000×8000（`MAX_COVER_SIZE`），超过按“最大封面”处理；
* 封面落盘保持原格式（`save_format=None` 时按图片自身格式存 PNG/JPEG）；
* `clear()` 是 Generator，逐个 `yield (已处理数, 总数, 当前项)` 供 UI 展示删除进度；
* 缓存里由后端记录本地可用的各个封面尺寸（`sizes` 列表），最大封面用图片真实尺寸作标记。

### MusicBackend（模板方法基类）
* `get_music_info(props, size, force_refresh)`：`from_cache -> from_web -> from_raw_smtc` 依次尝试，
  某一步返回 None 或抛异常就进入下一步；三步共用同一套 `(music_info, size)`，`music_info` 由
  原 SMTC 信息填充且**不含封面**；
* 派生类只实现 `from_cache` / `from_web` / `from_raw_smtc` 与 `encode_record` / `decode_record`；
* 约定：`size = -1`（`FULL_COVER_SIZE`）表示“获取最大封面”；
* `clear_cache()` 直接转发 `MusicCache.clear()` 的进度生成器。

## 函数迁移映射

### 从 `main.py::Plugin` 搬出

| 原位置 | 新位置 |
|---|---|
| `start_raw` 里建 SMTC / 取 SessionManager；`backend.create_smtc` | `SMTCHost.create()` |
| `stop_raw` 里对象置空 / 解绑 | `SMTCHost.destroy()` / `release_source()` |
| `update_smtc_info` | `SMTCHost.display()`（含 `app_media_id`/`start_fix`） |
| `default` 的写入部分 | `SMTCHost.display_default()` |
| `on_timeline_update`、`is_moe_koe_music` | `SMTCHost.update_timeline()`、`is_kugou_lyric_source()` |
| `on_button_press` 的会话操作 | `SMTCHost.handle_button()` |
| `check_source_valid`、`get_kugou_info`、播放状态同步 | `SMTCHost.check_source_valid()` / `get_source_info()` / `get_source_status()` |
| `on_session_changed` 里的 `get_kugou_session` | `SMTCHost.find_source()` |
| `load_cover`（歌名清洗 + 搜索 + 封面 URL + 缓存键） | `KugouMusicBackend.from_web()` + `clean_music_title` / `cover_url` / `music_hash_for` |
| `update_info`（下载 / 落盘 / 拼路径 / 读本地图 / 转流引用） | `KugouMusicBackend.from_web()`（下载+写缓存）、`MusicCache.find_cover()`（读缓存）、`SMTCHost._stream_reference()`（转流） |
| `load_cache` / `save_cache` / `remove_cache`（文件部分与进度） | `MusicBackend.load_cache()` / `save_cache()` / `clear_cache()`（进度 Generator） |
| `update_config` 中配置分发 | `Plugin._sync_config()` → `SMTCHost.bind()` + `MusicBackend.update_config()` |
| 音乐报告：`_handle_music_report` + `last_reporter_*` + `create_report` + `stop` 的 finish/save | `MusicReportManager.handle()` / `generate_report()` / `flush()` / `save()` |
| `main.py::MusicData` | 删除，由 `StandardMusicInfo.cover`（`PIL.Image.Image`）取代 |

### 从 `backend\base_backend.py` 搬出

| 原函数 | 新位置 |
|---|---|
| `wait_result` | `_smtc_backend_base.py` |
| `extract_music_title` / `search_for_song_list` / `search_music` | `_smtc_backend_kugou.py`（逻辑原样搬运） |
| `transform_to_url` | 删除（只被旧接口引用），由 `cover_url(template, size)` 取代 |
| `create_smtc` / `get_kugou_session` / `get_kugou_info` | `smtc_host.py` |
| 原 `CoverSource`（封面来源抽象） | 删除：查找/落盘能力并入 `MusicCache`，尺寸缩放与兜底并入 `MusicBackend` 模板流程 |

### 第二轮（模板方法改造）的合并

| 改造前 | 改造后 |
|---|---|
| `StandardMusicInfo` 含 `dump/load/cache_cover/load_cached_cover/fit_cover/...` | 纯数据；能力分别下沉到 `MusicCache` 与基类模板方法 |
| 各后端自己写“缓存 → 联网 → 兜底”三级顺序 | 统一由 `MusicBackend.get_music_info()` 驱动 |
| `CoverCache` + `MusicInfoCache` 两个类 | 合并为一个 `MusicCache` |
| `Plugin` 自己遍历文件删除缓存 | `MusicCache.clear()` 生成器 + `Plugin.remove_cache()` 展示进度 |

## 验收对照

| 验收项 | 证据 |
|---|---|
| 模板方法：缓存 → 联网 → 原 SMTC 低清封面 | 契约自验 C1–C8（顺序、force_refresh、某步抛异常继续）；端到端 E5–E30 |
| `StandardMusicInfo` 纯数据、不参与缓存 | 契约自验 D1/D2；集成 I31 |
| `MusicCache` 只做存储与查找、后端负责编解码 | 集成 I7；`encode_record`/`decode_record` 仅存在于 `KugouMusicBackend` |
| 封面尺寸规则（8000 硬限制、-1 最大封面、>= 请求尺寸优先、原格式落盘） | 契约自验 A2–A10、A15–A17、B1–B5 |
| 删除进度 Generator | 契约自验 A19–A22；端到端 E41/E42 |
| 新增后端只需子类 + 缓存目录 | 集成 I8–I11（`DummyBackend` 不实现流程即可用） |
| `Plugin` 不含实现细节 | 集成 I12/I13（逐词扫描 `main.py`） |
| 空 `album_title`/`album_artist`/`thumbnail` 不崩溃 | 契约 D3；端到端 E31–E35 |
| 配置同步（含平台切换） | 集成 I22–I28 |

## 已知取舍

- 缓存目录为 `cache\{platform}\`（歌曲信息 `music_info.json`，封面 `covers\`）；旧的 `cache\kugou_music_covers\` 与 `cache\kugou_cover_cache.json` 不再使用。
- 联网路径按“最大封面”下载并落盘（文件名用图片真实尺寸作标记），之后各尺寸请求复用同一份缓存。
- 缩略图统一转成 PIL 图片（`SMTCHost` 用 `InMemoryRandomAccessStream` 转回流引用）。
- `music_hash` 用 sha256 截断 64 位（`make_music_hash`）：`str.__hash__` 跨进程不稳定，不能做落盘缓存键。
- 缓存命中判定不依赖歌名（只按 `music_hash` 查，符合约定）；旧格式缓存会被视为未命中并自动联网重建。
- 本插件不再从 `plugins.HDKugouCover` 导入任何东西（`music_reporter.py` 用自身的 `Music`/`MusicReporter`）。
