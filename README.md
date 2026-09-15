# TSKX Catalog 远程资源浏览器

独立工具，用于解析游戏 **Twinkle Star Knights X** 自带的 Addressables `catalog.bundle`，把约 11 万个资源路径映射到 CDN 上的 bundle，并提供网页界面进行浏览、搜索、预览和版本对比。

不依赖任何外部 viewer 或本地缓存，直接解析游戏自带的明文 JSON catalog。

## 功能特性

- **Catalog 解析**：直接解析 Addressables 1.20.0 `ContentCatalogData` 二进制结构（`m_InternalIds` / `m_KeyDataString` / `m_EntryDataString` / `m_BucketDataString`），递归解析资源到 bundle 的依赖链。
- **资源浏览**：按路径关键字搜索、按分类（角色 Spine、BGM、语音、技能图片、CG 图鉴、扭蛋立绘等）和类型（图片/文本/Spine/音频）过滤，分页加载。
- **内联预览**：图片、文本资源在网页内直接预览；Spine 资源调用内置 Spine 4.0 WebGL 播放器渲染动画。
- **Bundle 下载**：远程 bundle 自动从 CDN 下载并缓存到本地 `catalog_cache/`，内置 bundle 直接读取游戏目录；支持下载整个 bundle。
- **版本快照与对比**：每次构建索引时自动保存一份全量快照，可在网页上选择两个快照进行 diff（新增 / 消失 / 换包）。

## 项目结构

```
tsk_catalog/
├── catalog_viewer.py        # 主程序（解码 + 索引 + HTTP 服务）
├── web/
│   ├── catalog.html         # 资源浏览 / 版本对比页面
│   ├── spine_player.html    # Spine 动画播放器页面
│   └── vendor/
│       └── spine-webgl-4.0.31.js
├── catalog_index.db         # SQLite 资源索引（自动生成）
├── catalog_cache/           # 远程 bundle 下载缓存（自动生成）
└── snapshots/               # 版本快照（自动生成）
```

## 环境依赖

- Python 3.8+
- [UnityPy](https://github.com/K0lb3/UnityPy)（用于解析 bundle 中的 TextAsset / Texture2D / Sprite）
- 标准库：`sqlite3`、`http.server`、`urllib` 等

安装依赖：

```bash
pip install UnityPy
```

## 配置

`catalog_viewer.py` 顶部硬编码了游戏路径与 CDN 地址，请按实际安装位置修改：

```python
GAME_DIR = Path(r"F:\Games\DMM\Twinkle_StarKnightsX")
AA_DIR = GAME_DIR / "twinkle_starknightsX_Data" / "StreamingAssets" / "aa"
CATALOG_BUNDLE = AA_DIR / "catalog.bundle"
STANDALONE_DIR = AA_DIR / "StandaloneWindows64"
REMOTE_CATALOG_JSON = "https://dz87n5pasv7ep.cloudfront.net/assetbundle-win/game/catalog_0.0.0.json"
CDN_BASE = "https://dz87n5pasv7ep.cloudfront.net/assetbundle-win/game/"
```

## 用法

```bash
# 首次运行：自动构建索引并启动服务（http://127.0.0.1:8771）
python catalog_viewer.py

# 仅构建索引（解析本地 catalog.bundle）
python catalog_viewer.py build

# 从 CDN 拉取最新 catalog JSON 后构建（同时生成版本快照）
python catalog_viewer.py build --remote

# 启动网页服务
python catalog_viewer.py serve

# 指定端口启动
python catalog_viewer.py serve --port 9000
```

启动后浏览器访问 <http://127.0.0.1:8771/> 即可。

## HTTP API

| 路径 | 说明 |
| --- | --- |
| `GET /api/stats` | 资源总数、bundle 数、catalog md5 |
| `GET /api/categories` | 分类列表及计数 |
| `GET /api/resources?q=&category=&kind=&bundle=&page=&size=` | 分页查询资源 |
| `GET /api/preview?path=&max_width=` | 图片/文本内联预览 |
| `GET /api/raw?path=` | 资源原始字节（供 Spine 播放器等消费） |
| `GET /api/spine_parts?path=` | 查询单个 Spine 单元的 skel / atlas / 贴图 |
| `GET /api/snapshots` | 列出全部版本快照 |
| `GET /api/diff?from=&to=&status=&category=&q=` | 两个快照之间的差异 |
| `GET /api/download_bundle?path=` | 下载资源所属的整个 bundle |

## Catalog 二进制结构参考

源码注释中保留了 Addressables 1.20.0 `ContentCatalogData` 的字段说明，便于二次开发或移植：

- `m_InternalIds`：前缀压缩的字符串数组（`"前缀索引#剩余路径"`）
- `m_KeyDataString`：`int32 数量 + N 个 (1 字节类型 + 载荷)`，类型 0=UTF8 / 1=UTF16 / 2=null / 4=int32 / 8=16 字节 GUID
- `m_EntryDataString`：`int32 数量 + N 条 28 字节定长记录`（7 个 int32：internalId / provider / dep_key / hash / bundle_data / main / rtype）
- `m_BucketDataString`：`int32 数量 + N 组 (first_entry, count, entry[count])`，把 key 关联到一组 entry
- 资源 → bundle：沿 `entry[2]` 依赖 key 递归 bucket 链，直到找到 `type=0` 的 http(s) bundle 条目

## 备注

- 首次构建索引会扫描全部 entry，耗时取决于机器性能（解析后写入 `catalog_index.db`，后续启动秒开）。
- 远程 bundle 首次访问时下载到 `catalog_cache/`，后续直接从缓存读取。
- Spine 运行时仅内置 4.0 版本；若资源版本不匹配，页面会提示下载 bundle 后用对应版本 Spine 编辑器查看。
- `catalog_index.db` 与 `snapshots/*.db` 为自动生成的中间产物，删除后可通过 `build` 重新生成。
