"""Twinkle Star Knights X - Addressables Catalog 远程资源浏览器（独立工具）。

不依赖 viewer.py / 本地缓存，直接解析游戏自带的 catalog.bundle（明文 JSON），
把 11 万个资源路径映射到 CDN 上的 bundle，并支持网页实时下载预览。

用法:
    python catalog_viewer.py                # 首次自动构建索引并启动服务
    python catalog_viewer.py build          # 仅构建索引（解析本地 catalog.bundle）
    python catalog_viewer.py build --remote # 从 CDN 拉取最新 catalog JSON 后构建
    python catalog_viewer.py serve          # 启动网页 (http://127.0.0.1:8771)
    python catalog_viewer.py serve --port 9000

catalog 二进制结构（Addressables 1.20.0 ContentCatalogData）:
    m_InternalIds      : 前缀压缩的字符串数组 ("前缀索引#剩余路径")
    m_KeyDataString    : int32 数量 + N 个 (1字节类型 + 载荷):
                         0=UTF8字符串(4字节长度) / 1=UTF16字符串 / 2=null /
                         4=int32 / 8=16字节(GUID)
    m_EntryDataString  : int32 数量 + N 条定长记录(7*int32, 共28字节):
                         [0]internalId索引 [1]provider [2]依赖key索引
                         [3]哈希 [4]bundle数据索引 [5]main索引 [6]资源类型索引
                         bundle 条目自身的 [2] == -1
    m_BucketDataString : int32 数量 + N 组 (first_entry:int32, count:int32,
                         entry索引:int32[count])，把 key 关联到一组 entry
    资源 -> bundle : entry[2] 是依赖 key 索引，递归 bucket 依赖链，
                     直到 type=0 的 http(s) bundle 条目
"""

import argparse
import base64
import hashlib
import io
import json
import os
import re
import sqlite3
import struct
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote

import UnityPy

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

GAME_DIR = Path(r"F:\Games\DMM\Twinkle_StarKnightsX")
AA_DIR = GAME_DIR / "twinkle_starknightsX_Data" / "StreamingAssets" / "aa"
CATALOG_BUNDLE = AA_DIR / "catalog.bundle"
STANDALONE_DIR = AA_DIR / "StandaloneWindows64"
REMOTE_CATALOG_JSON = "https://dz87n5pasv7ep.cloudfront.net/assetbundle-win/game/catalog_0.0.0.json"
CDN_BASE = "https://dz87n5pasv7ep.cloudfront.net/assetbundle-win/game/"

BASE_DIR = Path(__file__).parent
WEB_DIR = BASE_DIR / "web"
INDEX_DB = BASE_DIR / "catalog_index.db"
BUNDLE_CACHE_DIR = BASE_DIR / "catalog_cache"
SNAPSHOT_DIR = BASE_DIR / "snapshots"
DEFAULT_PORT = 8771

UA = {"User-Agent": "Mozilla/5.0"}

# ---------------------------------------------------------------------------
# 分类 / 类型
# ---------------------------------------------------------------------------

CATEGORY_RULES = [
    ("spine_chara", "角色Spine", re.compile(r"/characters/", re.I)),
    ("bgm", "音乐BGM", re.compile(r"/sound/bgm", re.I)),
    ("voice", "语音", re.compile(r"/sound/voice", re.I)),
    ("sound", "音效", re.compile(r"/sound/", re.I)),
    ("skill", "技能图片", re.compile(r"/sprites/skill", re.I)),
    ("picturebook", "CG图鉴", re.compile(r"/sprites/picturebook", re.I)),
    ("gacha", "扭蛋立绘", re.compile(r"/sprites/gacha", re.I)),
    ("chara_img", "角色图片", re.compile(r"/sprites/chara", re.I)),
    ("event", "活动插图", re.compile(r"/sprites/eventquest", re.I)),
    ("item", "道具图标", re.compile(r"/sprites/item", re.I)),
    ("equip", "装备图标", re.compile(r"/sprites/equip", re.I)),
    ("sprite", "其他图片", re.compile(r"/sprites/", re.I)),
    ("effect", "特效", re.compile(r"/effects?/", re.I)),
    ("prefab", "预制体", re.compile(r"/prefabs?/|\.prefab$", re.I)),
    ("shader", "着色器", re.compile(r"shader", re.I)),
]
CATEGORY_LABELS = dict(((k, l) for k, l, _ in CATEGORY_RULES))
CATEGORY_LABELS["other"] = "其他"


def classify(rel: str):
    for key, label, pattern in CATEGORY_RULES:
        if pattern.search(rel):
            return key
    return "other"


def kind_of(rel: str):
    """根据扩展名返回前端预览方式: image / text / spine / audio / other。"""
    low = rel.lower()
    if low.endswith((".png", ".jpg", ".jpeg", ".tga")):
        return "image"
    if low.endswith((".txt", ".json", ".csv", ".atlas", ".xml")):
        return "text"
    if low.endswith(".skel.bytes"):
        return "spine"
    if low.endswith((".wav", ".ogg")):
        return "audio"
    return "other"


def ext_of(rel: str):
    name = rel.rsplit("/", 1)[-1]
    if name.endswith(".skel.bytes"):
        return ".skel.bytes"
    return Path(name).suffix.lower()


# ---------------------------------------------------------------------------
# catalog 解码
# ---------------------------------------------------------------------------

def catalog_json_from_bundle(path: Path) -> bytes:
    """从 catalog.bundle 内的 TextAsset 无损取出 catalog JSON 字节。

    TextAsset 二进制布局: int32 名字长度 + 名字(4字节对齐) + int32 数据长度 + 数据
    """
    env = UnityPy.load(str(path))
    for obj in env.objects:
        if obj.type.name == "TextAsset":
            raw = obj.get_raw_data()
            name_len = struct.unpack_from("<i", raw, 0)[0]
            pos = (4 + name_len + 3) & ~3
            data_len = struct.unpack_from("<i", raw, pos)[0]
            return raw[pos + 4:pos + 4 + data_len]
    raise RuntimeError("catalog.bundle 中没有 TextAsset")


def decode_catalog(cat: dict):
    """解码 catalog，返回 (paths, entries, buckets, key_count)。

    paths[i]   : 第 i 个 internalId 的完整路径
    entries    : [(iid, provider, dep_key, hashv, bdata, main, rtype), ...]
    buckets[k] : 第 k 个 key 对应的 entry 索引列表
    """
    prefixes = cat["m_InternalIdPrefixes"]
    paths = []
    for s in cat["m_InternalIds"]:
        if "#" in s and s.split("#", 1)[0].isdigit():
            idx, rest = s.split("#", 1)
            paths.append(prefixes[int(idx)] + rest)
        else:
            paths.append(s)

    # ---- KeyData ----
    kd = base64.b64decode(cat["m_KeyDataString"])
    key_count = struct.unpack_from("<i", kd, 0)[0]
    keys = []
    q = 4
    while len(keys) < key_count:
        t = kd[q]
        q += 1
        if t == 0:  # UTF-8 字符串
            ln = struct.unpack_from("<i", kd, q)[0]
            q += 4
            keys.append(kd[q:q + ln].decode("utf-8", "replace"))
            q += ln
        elif t == 1:  # UTF-16 字符串
            ln = struct.unpack_from("<i", kd, q)[0]
            q += 4
            keys.append(kd[q:q + ln].decode("utf-16-le", "replace"))
            q += ln
        elif t == 2:  # null
            keys.append(None)
        elif t == 4:  # int32
            keys.append(struct.unpack_from("<i", kd, q)[0])
            q += 4
        elif t == 8:  # 16 字节（GUID/哈希）
            keys.append(kd[q:q + 16].hex())
            q += 16
        else:
            raise RuntimeError(f"未知 KeyData 类型 {t}（位置 key={len(keys)}）")

    # ---- BucketData ----
    bd = base64.b64decode(cat["m_BucketDataString"])
    bucket_count = struct.unpack_from("<i", bd, 0)[0]
    buckets = []
    q = 4
    for _ in range(bucket_count):
        # first_entry + count + entry 索引数组
        struct.unpack_from("<i", bd, q)[0]  # first_entry，建链时不需要
        cnt = struct.unpack_from("<i", bd, q + 4)[0]
        deps = list(struct.unpack_from("<%di" % cnt, bd, q + 8))
        buckets.append(deps)
        q += 8 + 4 * cnt

    # ---- EntryData ----
    ed = base64.b64decode(cat["m_EntryDataString"])
    entry_count = struct.unpack_from("<i", ed, 0)[0]
    entries = [struct.unpack_from("<7i", ed, 4 + i * 28) for i in range(entry_count)]

    return paths, entries, buckets, keys


def build_resolver(paths, entries, buckets):
    """返回 dep_key索引 -> bundle 完整路径(或None) 的带缓存解析函数。"""
    cache = {}

    def resolve(dep_key, seen=None):
        if dep_key in cache:
            return cache[dep_key]
        seen = seen if seen is not None else set()
        if dep_key in seen:
            return None
        seen.add(dep_key)
        result = None
        for entry_idx in buckets[dep_key]:
            iid = entries[entry_idx][0]
            sub_dep = entries[entry_idx][2]
            p = paths[iid]
            if p.endswith(".bundle"):
                result = p
                break
            if sub_dep >= 0:
                result = resolve(sub_dep, seen)
                if result:
                    break
        cache[dep_key] = result
        return result

    return resolve


# ---------------------------------------------------------------------------
# 索引构建（SQLite）
# ---------------------------------------------------------------------------

def build_index(use_remote: bool):
    if use_remote:
        print("正在从 CDN 下载最新 catalog JSON ...")
        req = urllib.request.Request(REMOTE_CATALOG_JSON, headers=UA)
        catalog_bytes = urllib.request.urlopen(req, timeout=300).read()
        source = REMOTE_CATALOG_JSON
    else:
        print(f"读取本地 {CATALOG_BUNDLE} ...")
        catalog_bytes = catalog_json_from_bundle(CATALOG_BUNDLE)
        source = str(CATALOG_BUNDLE)
    catalog_md5 = hashlib.md5(catalog_bytes).hexdigest()
    print(f"catalog {len(catalog_bytes) / 1048576:.1f} MB, md5={catalog_md5}")

    cat = json.loads(catalog_bytes)
    paths, entries, buckets, keys = decode_catalog(cat)
    resolve = build_resolver(paths, entries, buckets)
    print(f"internalId={len(paths)} entry={len(entries)} key={len(keys)}，开始建立资源映射 ...")

    rows = {}
    bundle_set = set()
    for iid, _provider, dep_key, _h, _bd, _main, _rtype in entries:
        p = paths[iid]
        if p.endswith(".bundle") or p.startswith("http") or dep_key < 0:
            continue
        if p in rows:
            continue
        bp = resolve(dep_key)
        if not bp:
            continue
        bundle_name = bp.rsplit("/", 1)[-1]
        remote = 1 if bp.startswith("http") else 0
        rel = p.split("AssetBundles/", 1)[-1] if "AssetBundles/" in p else p
        rows[p] = (
            p, rel, rel.rsplit("/", 1)[-1], ext_of(rel), kind_of(rel),
            classify(p), bundle_name[:-7] if bundle_name.endswith(".bundle") else bundle_name,
            remote,
        )
        bundle_set.add((bundle_name, remote))

    tmp_db = INDEX_DB.with_suffix(".db.tmp")
    if tmp_db.exists():
        tmp_db.unlink()
    db = sqlite3.connect(str(tmp_db))
    db.execute("""CREATE TABLE resources(
        path TEXT PRIMARY KEY, rel TEXT, name TEXT, ext TEXT, kind TEXT,
        category TEXT, bundle_hex TEXT, remote INTEGER)""")
    db.execute("CREATE INDEX idx_cat ON resources(category)")
    db.execute("CREATE INDEX idx_hex ON resources(bundle_hex)")
    db.executemany("INSERT INTO resources VALUES (?,?,?,?,?,?,?,?)", list(rows.values()))
    db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    db.execute("INSERT INTO meta VALUES ('catalog_md5', ?)", (catalog_md5,))
    db.execute("INSERT INTO meta VALUES ('source', ?)", (source,))
    db.execute("INSERT INTO meta VALUES ('bundle_count', ?)", (str(len(bundle_set)),))
    remote_bundles = sum(1 for _, r in bundle_set if r)
    db.execute("INSERT INTO meta VALUES ('remote_bundle_count', ?)", (str(remote_bundles),))
    db.commit()
    db.close()
    os.replace(tmp_db, INDEX_DB)
    print(f"完成: {len(rows)} 个资源, {len(bundle_set)} 个 bundle（远程 {remote_bundles}）")
    print(f"索引: {INDEX_DB}")
    create_snapshot(catalog_md5, source, len(rows))


# ---------------------------------------------------------------------------
# 版本快照 / diff
# ---------------------------------------------------------------------------

def snapshot_path(md5: str) -> Path:
    return SNAPSHOT_DIR / f"snap_{md5}.db"


def create_snapshot(md5: str, source: str, resource_count: int):
    """把当前索引全量复制为独立快照（同 md5 已存在则跳过）。"""
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    sp = snapshot_path(md5)
    if sp.exists():
        print(f"快照已存在，跳过: {sp.name}")
        return
    tmp = sp.with_suffix(".db.tmp")
    if tmp.exists():
        tmp.unlink()
    sdb = sqlite3.connect(str(tmp))
    sdb.execute("""CREATE TABLE resources(
        path TEXT PRIMARY KEY, rel TEXT, name TEXT, category TEXT,
        kind TEXT, bundle_hex TEXT, remote INTEGER)""")
    src = sqlite3.connect(str(INDEX_DB))
    data = src.execute(
        "SELECT path, rel, name, category, kind, bundle_hex, remote FROM resources"
    ).fetchall()
    src.close()
    sdb.executemany("INSERT INTO resources VALUES (?,?,?,?,?,?,?)", data)
    sdb.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    sdb.execute("INSERT INTO meta VALUES ('source', ?)", (source,))
    sdb.execute("INSERT INTO meta VALUES ('resources', ?)", (str(resource_count),))
    sdb.commit()
    sdb.close()
    os.replace(tmp, sp)
    print(f"版本快照已保存: {sp.name}（{resource_count} 个资源）")


def list_snapshots():
    if not SNAPSHOT_DIR.exists():
        return []
    out = []
    files = sorted(SNAPSHOT_DIR.glob("snap_*.db"), key=lambda x: x.stat().st_mtime)
    for f in files:
        md5 = f.stem[5:]
        conn = sqlite3.connect(str(f))
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        n = conn.execute("SELECT count(*) FROM resources").fetchone()[0]
        conn.close()
        out.append({
            "id": md5,
            "created_at": int(f.stat().st_mtime),
            "source": meta.get("source", ""),
            "resources": int(meta.get("resources", n)),
        })
    return out


_diff_cache = {}
_diff_cache_lock = threading.Lock()


def compute_diff(from_md5: str, to_md5: str):
    """返回 {path: (status, from_hex, to_hex, rel, name, category, kind, remote)}。

    status: added / removed / changed
    """
    cache_key = (from_md5, to_md5)
    with _diff_cache_lock:
        cached = _diff_cache.get(cache_key)
    if cached is not None:
        return cached

    def load(md5):
        conn = sqlite3.connect(str(snapshot_path(md5)))
        rows = {r[0]: r for r in conn.execute(
            "SELECT path, rel, name, category, kind, bundle_hex, remote FROM resources")}
        conn.close()
        return rows

    old = load(from_md5)
    new = load(to_md5)
    result = {}
    for path, (_p, rel, name, category, kind, hexv, remote) in new.items():
        old_row = old.get(path)
        if old_row is None:
            result[path] = ("added", None, hexv, rel, name, category, kind, remote)
        elif old_row[5] != hexv:
            result[path] = ("changed", old_row[5], hexv, rel, name, category, kind, remote)
    for path, (_p, rel, name, category, kind, hexv, remote) in old.items():
        if path not in new:
            result[path] = ("removed", hexv, None, rel, name, category, kind, remote)

    with _diff_cache_lock:
        _diff_cache[cache_key] = result
    return result


def ensure_index():
    if not INDEX_DB.exists():
        print("索引不存在，先构建 ...")
        build_index(use_remote=False)


# ---------------------------------------------------------------------------
# bundle 获取（磁盘缓存）+ 资源解码
# ---------------------------------------------------------------------------

_cache_locks_guard = threading.Lock()
_cache_locks = {}


def _bundle_lock(hexname: str):
    with _cache_locks_guard:
        lk = _cache_locks.get(hexname)
        if lk is None:
            lk = threading.Lock()
            _cache_locks[hexname] = lk
        return lk


# bundle 下载进度：hexname -> {status, loaded, total}
# status: downloading / done / error（重启后清空，只服务于实时展示）
_bundle_progress = {}
_bundle_progress_lock = threading.Lock()


def _set_bundle_progress(hexname: str, **fields):
    with _bundle_progress_lock:
        _bundle_progress.setdefault(hexname, {}).update(fields)


def get_bundle_progress(hexname: str) -> dict:
    with _bundle_progress_lock:
        return dict(_bundle_progress.get(hexname) or {})


def get_bundle_bytes(hexname: str, remote: bool):
    """返回 bundle 字节。远程包分块下载并缓存到 catalog_cache/，同时更新下载进度。"""
    local_path = BUNDLE_CACHE_DIR / f"{hexname}.bundle"
    if local_path.exists() and local_path.stat().st_size > 0:
        return local_path.read_bytes(), True
    if remote:
        lk = _bundle_lock(hexname)
        with lk:
            if local_path.exists() and local_path.stat().st_size > 0:
                return local_path.read_bytes(), True
            url = CDN_BASE + quote(hexname) + ".bundle"
            req = urllib.request.Request(url, headers=UA)
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    total = int(resp.headers.get("Content-Length", 0) or 0)
                    _set_bundle_progress(hexname, status="downloading",
                                         loaded=0, total=total)
                    chunks = []
                    loaded = 0
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        chunks.append(chunk)
                        loaded += len(chunk)
                        _set_bundle_progress(hexname, loaded=loaded)
                    blob = b"".join(chunks)
            except Exception:
                _set_bundle_progress(hexname, status="error")
                raise
            BUNDLE_CACHE_DIR.mkdir(exist_ok=True)
            tmp = local_path.with_suffix(".bundle.tmp")
            tmp.write_bytes(blob)
            os.replace(tmp, local_path)
            _set_bundle_progress(hexname, status="done",
                                 loaded=len(blob), total=total or len(blob))
            return blob, False
    # 内置 bundle：尝试 StandaloneWindows64
    builtin = STANDALONE_DIR / f"{hexname}.bundle"
    if builtin.exists():
        return builtin.read_bytes(), True
    raise FileNotFoundError(f"内置 bundle 不存在: {hexname}")


def read_textasset_bytes(obj):
    """无损解析 TextAsset: AlignedString name + int32 长度 + 字节。"""
    raw = obj.get_raw_data()
    name_len = struct.unpack_from("<i", raw, 0)[0]
    pos = (4 + name_len + 3) & ~3
    data_len = struct.unpack_from("<i", raw, pos)[0]
    name = raw[4:4 + name_len].decode("utf-8", "replace")
    return name, raw[pos + 4:pos + 4 + data_len]


def _reader_for_path(env, path: str):
    """env.container 的值是 PPtr，需要按 path_id 找回 ObjectReader。

    注意该 UnityPy 版本的 container 不支持 .get()，只能遍历。
    """
    target_pptr = None
    for cpath, pptr in env.container.items():
        if cpath == path:
            target_pptr = pptr
            break
    if target_pptr is None:
        return None
    pid = target_pptr.path_id
    for obj in env.objects:
        if obj.path_id == pid:
            return obj
    return None


def preview_resource(row, max_width: int):
    """下载/读取 bundle 并解码指定资源，返回 (body, content_type, extra)。"""
    path, rel, name, ext, kind, category, hexname, remote = row
    blob, _cached = get_bundle_bytes(hexname, bool(remote))
    env = UnityPy.load(io.BytesIO(blob))

    if kind == "image":
        target = _reader_for_path(env, path)
        if target is None or target.type.name not in ("Texture2D", "Sprite"):
            for obj in env.objects:
                if obj.type.name in ("Texture2D", "Sprite"):
                    target = obj
                    break
        if target is None:
            raise RuntimeError("bundle 内没有贴图")
        data = target.read()
        img = data.image
        if max_width and img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, max(1, int(img.height * ratio))))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue(), "image/png", {"size": list(img.size)}

    if kind == "text":
        target = _reader_for_path(env, path)
        if target is None or target.type.name != "TextAsset":
            for obj in env.objects:
                if obj.type.name == "TextAsset":
                    target = obj
                    break
        if target is None:
            raise RuntimeError("bundle 内没有 TextAsset")
        _tn, text_blob = read_textasset_bytes(target)
        return text_blob, "text/plain; charset=utf-8", {}

    # spine / audio / other：不做内联预览，交由前端提示下载
    raise UnsupportedPreview(kind)


def raw_resource(row):
    """返回资源的原始/无损字节 (body, content_type)，供 Spine 播放器等直接消费。"""
    path, rel, name, ext, kind, category, hexname, remote = row
    blob, _cached = get_bundle_bytes(hexname, bool(remote))
    env = UnityPy.load(io.BytesIO(blob))

    if kind == "image":
        target = _reader_for_path(env, path)
        if target is None or target.type.name not in ("Texture2D", "Sprite"):
            for obj in env.objects:
                if obj.type.name in ("Texture2D", "Sprite"):
                    target = obj
                    break
        if target is None:
            raise RuntimeError("bundle 内没有贴图")
        buf = io.BytesIO()
        target.read().image.save(buf, "PNG")
        return buf.getvalue(), "image/png"

    if kind in ("text", "spine"):
        target = _reader_for_path(env, path)
        if target is None or target.type.name != "TextAsset":
            for obj in env.objects:
                if obj.type.name == "TextAsset":
                    target = obj
                    break
        if target is None:
            raise RuntimeError("bundle 内没有 TextAsset")
        _tn, text_blob = read_textasset_bytes(target)
        if kind == "spine":
            return text_blob, "application/octet-stream"
        return text_blob, "text/plain; charset=utf-8"

    raise UnsupportedPreview(kind)


def spine_parts(skel_path: str):
    """根据 .skel.bytes 路径，在索引中找齐同单元的 skel/atlas/png（纯索引查询，不下载）。"""
    if not skel_path.endswith(".skel.bytes"):
        raise ValueError("path 必须是 .skel.bytes 资源")
    directory = skel_path.rsplit("/", 1)[0]
    stem = skel_path.rsplit("/", 1)[-1][:-len(".skel.bytes")]

    db = db_connect()
    try:
        def one(suffix, kind=None):
            sql = "SELECT path, rel, kind, bundle_hex FROM resources WHERE path = ?"
            r = db.execute(sql, (directory + "/" + stem + suffix,)).fetchone()
            return dict(r) if r else None

        skel = one(".skel.bytes")
        atlas = one(".atlas.txt")
        if skel is None:
            raise FileNotFoundError(f"索引中没有 {skel_path}")
        # 贴图：同 bundle 内全部图片（一个 spine 单元通常一张；多页 atlas 也能覆盖）
        textures = [dict(r) for r in db.execute(
            "SELECT path, rel, kind, bundle_hex FROM resources "
            "WHERE bundle_hex = ? AND kind = 'image' ORDER BY path",
            (skel["bundle_hex"],)).fetchall()]
    finally:
        db.close()

    def to_part(r):
        if r is None:
            return None
        return {"path": r["path"], "rel": r["rel"],
                "url": "/api/raw?path=" + quote(r["path"])}

    return {
        "name": stem,
        "bundle_hex": skel["bundle_hex"],
        "skel": to_part(skel),
        "atlas": to_part(atlas),
        "textures": [to_part(t) for t in textures],
    }


class UnsupportedPreview(Exception):
    pass


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------

def db_connect():
    conn = sqlite3.connect(str(INDEX_DB))
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# 角色图鉴聚合
#
# 资源里的角色 ID 为 7 位（如 1001005），wiki 等资料站使用的 6 位 ID 是去掉
# 首位后的结果（1001005 -> 001005）。每个角色通常带 b/c/f/m0/m1 五个 Spine
# 变体；头像使用 Sprites/Chara/Thumb_* 下游戏自带的缩略图，无需外链 wiki。
# ---------------------------------------------------------------------------

_CH_SKEL_RE = re.compile(r"^ch_(\d{7})_([a-z0-9]+)\.skel\.bytes$")
_CHARA_IMG_RE = re.compile(r"^chara_(\d{7})_(\d+)_(\d+)\.png$")
_CHARACTER_CACHE = {"md5": None, "items": None}

# 头像选图优先级里的变体后缀排名（_2_1 是常规头像，数量最多）
_SUFFIX_RANK = {(2, 1): 0, (1, 1): 1, (2, 2): 2}


def build_character_index():
    """聚合角色列表：Spine 变体 / R18 标记 / m0 播放信息 / 最优头像路径。"""
    db = db_connect()
    try:
        row = db.execute("SELECT value FROM meta WHERE key='catalog_md5'").fetchone()
        md5 = row[0] if row else ""
        if _CHARACTER_CACHE["md5"] == md5 and _CHARACTER_CACHE["items"] is not None:
            return _CHARACTER_CACHE["items"]

        chars = {}

        def get(cid):
            c = chars.get(cid)
            if c is None:
                c = {"id": cid, "wiki_id": cid[1:], "variants": set(),
                     "adult": False, "spine_bundles": {}, "_thumb": None}
                chars[cid] = c
            return c

        # 1) 高清 general 版 Spine（变体集合 + 各变体播放所需 bundle 信息）
        rows = db.execute(
            "SELECT name, bundle_hex, remote FROM resources "
            "WHERE category='spine_chara' AND kind='spine' AND path LIKE ?",
            ("Assets/AssetBundles/Characters/HighQuality/general/%",)).fetchall()
        for r in rows:
            m = _CH_SKEL_RE.match(r["name"])
            if not m or m.group(1) == "0000000":
                continue
            cid, variant = m.group(1), m.group(2)
            c = get(cid)
            c["variants"].add(variant)
            c["spine_bundles"][variant] = (r["bundle_hex"], r["remote"])

        # 2) 是否存在 R18 版 Spine
        rows = db.execute(
            "SELECT DISTINCT name FROM resources "
            "WHERE category='spine_chara' AND kind='spine' AND path LIKE ?",
            ("Assets/AssetBundles/Characters/HighQuality/adult/%",)).fetchall()
        for r in rows:
            m = _CH_SKEL_RE.match(r["name"])
            if m and m.group(1) != "0000000":
                get(m.group(1))["adult"] = True

        # 3) 头像：(general 优先, Thumb 编号升序, _2_1 优先) 取分最低者
        rows = db.execute(
            "SELECT path, rel, name FROM resources "
            "WHERE category='chara_img' AND kind='image' "
            "AND rel LIKE 'Sprites/Chara/Thumb%'").fetchall()
        for r in rows:
            m = _CHARA_IMG_RE.match(r["name"])
            if not m or m.group(1) == "0000000":
                continue
            parts = r["rel"].split("/")
            is_adult = "adult" in parts
            thumb_no = 999
            for seg in parts:
                if seg.startswith("Thumb_"):
                    try:
                        thumb_no = int(seg.split("_")[1])
                    except (IndexError, ValueError):
                        pass
            rank = _SUFFIX_RANK.get((int(m.group(2)), int(m.group(3))), 3)
            score = (1 if is_adult else 0, thumb_no, rank)
            c = get(m.group(1))
            if c["_thumb"] is None or score < c["_thumb"][0]:
                c["_thumb"] = (score, r["path"])

        # 播放优先级：m0 → m1 → b → f → c（1 系角色普遍带 m0；
        # 部分 2xx/3xx 只有 m1 或 b，按此顺序回退）
        play_priority = ("m0", "m1", "b", "f", "c")

        items = []
        for cid in sorted(chars):
            c = chars[cid]
            c["variants"] = sorted(c["variants"])
            c["thumb_path"] = c.pop("_thumb")[1] if c["_thumb"] else ""
            bundles = c.pop("spine_bundles")
            # 返回每个变体的播放信息，前端可自由选择
            c["spine_variants"] = [
                {"variant": v,
                 "path": ("Assets/AssetBundles/Characters/HighQuality/general/"
                          f"ch_{cid}/ch_{cid}_{v}.skel.bytes"),
                 "bundle": bundles[v][0],
                 "remote": bundles[v][1]}
                for v in c["variants"] if v in bundles
            ]
            play = next((v for v in play_priority if v in bundles), None)
            c["play_variant"] = play or ""
            items.append(c)

        _CHARACTER_CACHE["md5"] = md5
        _CHARACTER_CACHE["items"] = items
        return items
    finally:
        db.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "CatalogViewer/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, body, content_type, filename=None, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if filename:
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{quote(filename)}")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, rel):
        fp = (WEB_DIR / rel).resolve()
        if not str(fp).startswith(str(WEB_DIR.resolve())) or not fp.is_file():
            self.send_error(404)
            return
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8"}.get(fp.suffix, "application/octet-stream")
        if fp.suffix in (".html", ".js", ".css"):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            body = fp.read_bytes()
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return
        self._bytes(fp.read_bytes(), ctype)

    def do_GET(self):
        parsed = urlparse(self.path)
        route, qs = parsed.path, parse_qs(parsed.query)
        try:
            if route in ("/", "/catalog.html"):
                return self._static("catalog.html")
            if route == "/spine_player.html":
                return self._static("spine_player.html")
            if route.startswith("/vendor/"):
                return self._static(route.lstrip("/"))
            if route == "/api/stats":
                return self.api_stats()
            if route == "/api/categories":
                return self.api_categories()
            if route == "/api/characters":
                return self.api_characters(qs)
            if route == "/api/resources":
                return self.api_resources(qs)
            if route == "/api/preview":
                return self.api_preview(qs)
            if route == "/api/raw":
                return self.api_raw(qs)
            if route == "/api/spine_parts":
                return self.api_spine_parts(qs)
            if route == "/api/bundle_progress":
                return self.api_bundle_progress(qs)
            if route == "/api/snapshots":
                return self.api_snapshots()
            if route == "/api/diff":
                return self.api_diff(qs)
            if route == "/api/download_bundle":
                return self.api_download_bundle(qs)
            self.send_error(404)
        except UnsupportedPreview as exc:
            self._json({"error": f"该资源类型（{exc.args[0]}）不支持网页预览，请下载 bundle",
                        "kind": exc.args[0]}, 415)
        except FileNotFoundError as exc:
            self._json({"error": str(exc)}, 404)
        except Exception as exc:
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    # -- API ---------------------------------------------------------------
    def api_stats(self):
        db = db_connect()
        try:
            total = db.execute("SELECT count(*) FROM resources").fetchone()[0]
            bundles = db.execute("SELECT count(DISTINCT bundle_hex) FROM resources").fetchone()[0]
            meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
        finally:
            db.close()
        self._json({"resources": total, "bundles": bundles, **meta})

    def api_categories(self):
        db = db_connect()
        try:
            rows = db.execute(
                "SELECT category, count(*) c FROM resources GROUP BY category ORDER BY c DESC"
            ).fetchall()
        finally:
            db.close()
        self._json([{"key": r["category"], "label": CATEGORY_LABELS.get(r["category"], r["category"]),
                     "count": r["c"]} for r in rows])

    # -- 角色图鉴 -----------------------------------------------------------
    def api_characters(self, qs):
        q = qs.get("q", [""])[0].strip()
        only_spine = qs.get("spine", [""])[0] == "1"
        only_adult = qs.get("adult", [""])[0] == "1"
        # 默认只看 1xxxxxx（wiki 角色一覧对应的可抽取角色）；all=1 时包含
        # 2xxxxxx / 3xxxxxx 等特殊与剧情模型
        show_all = qs.get("all", [""])[0] == "1"
        page = max(1, int(qs.get("page", ["1"])[0] or 1))
        size = min(200, max(10, int(qs.get("size", ["60"])[0] or 60)))

        items = build_character_index()
        filtered = []
        for c in items:
            if not show_all and c["id"][0] != "1":
                continue
            if only_spine and not c["variants"]:
                continue
            if only_adult and not c["adult"]:
                continue
            if q:
                # 纯 6 位数字视为 wiki ID，只匹配 1 系；其余按子串匹配
                if re.fullmatch(r"\d{6}", q):
                    if not (c["id"][0] == "1" and c["wiki_id"] == q):
                        continue
                elif q not in c["id"] and q not in c["wiki_id"]:
                    continue
            filtered.append(c)
        total = len(filtered)
        start = (page - 1) * size
        self._json({"total": total, "page": page, "size": size,
                    "items": filtered[start:start + size]})

    def api_resources(self, qs):
        q = (qs.get("q", [""])[0].strip())
        cat = qs.get("category", [""])[0].strip()
        kind = qs.get("kind", [""])[0].strip()
        hexname = qs.get("bundle", [""])[0].strip()
        page = max(1, int(qs.get("page", ["1"])[0] or 1))
        size = min(200, max(10, int(qs.get("size", ["100"])[0] or 100)))

        where, args = [], []
        if q:
            where.append("rel LIKE ?")
            args.append(f"%{q}%")
        if cat:
            where.append("category = ?")
            args.append(cat)
        if kind:
            where.append("kind = ?")
            args.append(kind)
        if hexname:
            where.append("bundle_hex = ?")
            args.append(hexname)
        clause = (" WHERE " + " AND ".join(where)) if where else ""

        db = db_connect()
        try:
            total = db.execute(f"SELECT count(*) FROM resources{clause}", args).fetchone()[0]
            rows = db.execute(
                f"SELECT path, rel, name, ext, kind, category, bundle_hex, remote "
                f"FROM resources{clause} ORDER BY path LIMIT ? OFFSET ?",
                args + [size, (page - 1) * size]).fetchall()
        finally:
            db.close()
        items = []
        for r in rows:
            d = dict(r)
            d["category_label"] = CATEGORY_LABELS.get(d["category"], d["category"])
            items.append(d)
        self._json({
            "total": total, "page": page, "size": size,
            "items": items,
        })

    def _row_by_path(self, path):
        db = db_connect()
        try:
            r = db.execute("SELECT * FROM resources WHERE path = ?", (path,)).fetchone()
        finally:
            db.close()
        if r is None:
            raise FileNotFoundError(f"索引中没有该资源: {path}")
        return tuple(r)

    def api_preview(self, qs):
        path = qs.get("path", [""])[0]
        if not path:
            return self._json({"error": "缺少 path 参数"}, 400)
        max_width = int(qs.get("max_width", ["1600"])[0] or 1600)
        row = self._row_by_path(path)
        body, ctype, extra = preview_resource(row, max_width)
        # 额外元信息通过自定义头返回
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=3600")
        if extra.get("size"):
            self.send_header("X-Image-Size", "%dx%d" % tuple(extra["size"]))
        self.end_headers()
        self.wfile.write(body)

    def api_download_bundle(self, qs):
        path = qs.get("path", [""])[0]
        if not path:
            return self._json({"error": "缺少 path 参数"}, 400)
        row = self._row_by_path(path)
        _p, rel, _n, _e, kind, _cat, hexname, remote = row
        blob, cached = get_bundle_bytes(hexname, bool(remote))
        self._bytes(blob, "application/octet-stream", filename=f"{hexname}.bundle")

    def api_raw(self, qs):
        path = qs.get("path", [""])[0]
        if not path:
            return self._json({"error": "缺少 path 参数"}, 400)
        row = self._row_by_path(path)
        body, ctype = raw_resource(row)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def api_spine_parts(self, qs):
        path = qs.get("path", [""])[0]
        if not path:
            return self._json({"error": "缺少 path 参数"}, 400)
        self._json(spine_parts(path))

    def api_bundle_progress(self, qs):
        hexname = qs.get("hex", [""])[0].strip()
        if not hexname:
            return self._json({"error": "缺少 hex 参数"}, 400)
        progress = get_bundle_progress(hexname)
        if not progress:
            # 没有记录：可能已缓存/内置（无需下载），或下载尚未开始
            self._json({"status": "unknown"})
            return
        self._json(progress)

    def api_snapshots(self):
        self._json(list_snapshots())

    def api_diff(self, qs):
        from_md5 = qs.get("from", [""])[0].strip()
        to_md5 = qs.get("to", [""])[0].strip()
        status = qs.get("status", [""])[0].strip()
        cat = qs.get("category", [""])[0].strip()
        q = qs.get("q", [""])[0].strip()
        page = max(1, int(qs.get("page", ["1"])[0] or 1))
        size = min(500, max(10, int(qs.get("size", ["100"])[0] or 100)))
        if not from_md5 or not to_md5:
            return self._json({"error": "缺少 from/to 快照参数"}, 400)
        if not snapshot_path(from_md5).exists() or not snapshot_path(to_md5).exists():
            return self._json({"error": "快照不存在，请先 build 生成"}, 404)

        diff = compute_diff(from_md5, to_md5)
        summary = {"added": 0, "removed": 0, "changed": 0}
        filtered = []
        for p, (st, from_hex, to_hex, rel, name, category, kind, remote) in diff.items():
            summary[st] += 1
            if status and st != status:
                continue
            if cat and category != cat:
                continue
            if q and q.lower() not in rel.lower():
                continue
            filtered.append({
                "path": p, "rel": rel, "name": name, "status": st,
                "category": category,
                "category_label": CATEGORY_LABELS.get(category, category),
                "kind": kind, "from_hex": from_hex, "to_hex": to_hex,
                "remote": remote,
            })
        filtered.sort(key=lambda x: x["rel"])
        total = len(filtered)
        start = (page - 1) * size
        self._json({
            "from": from_md5, "to": to_md5, "summary": summary,
            "total": total, "page": page, "size": size,
            "items": filtered[start:start + size],
        })


def main():
    ap = argparse.ArgumentParser(description="TSKX Addressables catalog 远程资源浏览器")
    sub = ap.add_subparsers(dest="cmd")
    p_build = sub.add_parser("build", help="构建资源索引")
    p_build.add_argument("--remote", action="store_true", help="从 CDN 拉取最新 catalog")
    p_serve = sub.add_parser("serve", help="启动网页服务")
    p_serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()

    if args.cmd == "build":
        build_index(use_remote=args.remote)
        return
    if args.cmd == "serve":
        ensure_index()
        port = args.port
    else:
        ensure_index()
        port = DEFAULT_PORT

    BUNDLE_CACHE_DIR.mkdir(exist_ok=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Catalog 浏览器已启动: http://127.0.0.1:{port}/")
    print("按 Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
