# Graphionica 4.6.1 — 协议文档（字段级）

Base URL：`https://graphionica-app.firebaseapp.com/api/v2/`
方法：**全部 POST**，`Content-Type: application/x-www-form-urlencoded`
鉴权：**无**（无 Authorization / 无 apikey / 无签名 / 无设备指纹）
实测行为：GET 同一路径返回 `Cannot GET /api/v2/xxx`（Express 默认 404 页），说明前端是 Firebase
Hosting 代理到自建 Express 服务。

字段缩写语义来自反编译模型类的 `@SerializedName` 注解（Gson 混淆为 `@q9.b`），不是猜的：

| 模型 | 文件 |
|------|------|
| `SResponse` / `HomeResponse` / `TResponse` | `com.trimf.insta.d.m.s.*` / `com.trimf.insta.d.m.t.TResponse` |
| `S`（贴纸）、`SP`（贴纸包） | `com.trimf.insta.d.m.s.S` / `.SP` |
| `T`（模板）、`TP`（模板包） | `com.trimf.insta.d.m.t.T` / `.TP` |
| `TXTTemplateItem`（文字图层） | `com.trimf.insta.d.m.t.templateItem.TXTTemplateItem` |

---

## 1. 统一信封

三个端点共用信封字段：

| 字段 | 类型 | 含义 |
|------|------|------|
| `u` | string | 主 CDN 前缀（DigitalOcean Spaces，`fra1`） |
| `u2` | string | 镜像 CDN 前缀（Yandex Object Storage） |
| `v` | int | 该目录当前版本号 |
| `isV` | bool | `version == v` 时为 `false`（"你的版本已是最新"），否则 `true` 并下发全量 |
| 其余 | list | 具体目录列表，`isV=false` 时为空数组 |

请求字段（三个端点共有）：

| form 字段 | 类型 | 说明 |
|-----------|------|------|
| `version` | int | 客户端已持有的目录版本；`0` 表示全量拉取 |
| `platform` | string | `android` |
| `versionCode` | int | 客户端版本码，4.6.1 = `418` |
| `language` | string | 实测**不影响内容**（传 `en` / 长垃圾串返回一致） |
| `country` | string | 同上 |

`/templates` 额外有：

| form 字段 | 类型 | 说明 |
|-----------|------|------|
| `stickersVersion` | int | 客户端已持有的贴纸目录版本（贴纸与模板版本互相独立） |

---

## 2. `POST /home`

```bash
curl -X POST https://graphionica-app.firebaseapp.com/api/v2/home \
  -d 'version=0&platform=android&versionCode=418&language=en&country=US'
```

返回（`v=3`，9 条 banner）：

| 字段 | 类型 | 含义 |
|------|------|------|
| `b` | list | 运营 banner 列表 |
| `b[].id` | int | banner id |
| `b[].o` | int | 排序 |
| `b[].type` | string | 落地类型：`SP`（贴纸包） / `TP`（模板包） |
| `b[].t` | string | 标题 |
| `b[].s` | string | 副标题（常为空） |
| `b[].b` | string | banner 图路径（相对 `u`，形如 `b/23.webp`） |
| `b[].d` | string | 目标任务 id（字符串数字，指向 `SP`/`TP` 的 id） |

## 3. `POST /stickers`

返回 `v=211`：`sP` 48 个贴纸包、`s` 1682 张贴纸。

`sP[]`（`com.trimf.insta.d.m.s.SP`）：

| 字段 | 类型 | 含义 |
|------|------|------|
| `id` | long | 包 id |
| `o` | int | 排序 |
| `n` | string | 包名（英文，不随 `language` 变） |
| `p` | string | 包预览图路径 |
| `tV` | string | tab 缩略图路径 |
| `cT` | bool | 是否带颜色 tab（colorTab） |
| `isN` | int \| null | isNew 标记 |
| `isP` | **int** | **isPopular：热门度分值**（注意：包级 `isP` 不是 premium！） |
| `aT` | int | 作者类型 |
| `a` | string | 作者标识（`graphionica`） |

`s[]`（`com.trimf.insta.d.m.s.S`）：

| 字段 | 类型 | 含义 |
|------|------|------|
| `id` | long | 贴纸 id |
| `o` | long | 包内排序 |
| `pId` | long | 所属包 id（= `sP[].id`） |
| `u` | string | 贴纸本体路径（如 `1_flowers/1.webp`） |
| `p` | string | 预览图路径（`1_flowers/preview/1.webp`） |
| `w` / `h` | int | 原始宽高（px） |
| `isP` | **bool** | **isPremium：是否付费贴纸** |
| `a` | bool | antialias（渲染抗锯齿开关） |
| `c` | bool | color（是否单色可改色） |

计数实测：`isP=true` **1215 / 1682**，`a=true`（动图 webp）**312**。

## 4. `POST /templates`

返回 `v=161`：`tP` 29 个模板包、`t` 568 套模板、`tS` 463 个模板内贴纸。

`tP[]`：`id / o / n / p / isN / aT / a`（同 `SP`，无 `tV`，无 `isP`）

`t[]`（`com.trimf.insta.d.m.t.T`）：

| 字段 | 类型 | 含义 |
|------|------|------|
| `id` | long | 模板 id |
| `o` | long | 排序 |
| `pId` | long | 所属模板包 id |
| `p` | string | 预览图/底图路径（如 `1_base/1.webp`） |
| `d` | string | 画布比例：`9x16` / `1x1` / `16x9` … |
| `c` | string | 画布背景色（`#rrggbb`） |
| `isP` | **bool** | **isPremium** |
| `pO` | int \| null | **popularOrder：热门排序**（不是图层数！） |
| `i` | list | **图层数组**（真正的模板结构，见下） |

计数实测：`isP=true` **489 / 568**。

`i[]` 图层（多态，用 `tit` 区分；`tit="txt"` 时字段最全）：

| 字段 | 含义 |
|------|------|
| `tit` | 图层类型：`txt`（文字） / `m`（媒体占位） / … |
| `tX` / `tY` | 相对画布中心的偏移（px） |
| `r` / `rX` / `rY` | 旋转与旋转锚点 |
| `a` | 透明度 |
| `c` | 颜色（文字色或占位底色） |
| `w` / `h` | 尺寸（媒体占位图层） |
| `l` / `nA` | 锁定 / 不透明度动画等开关 |
| `m` / `s` | 蒙版 / 形状引用（多为 null） |
| `fId` | **字体：运行时 `R.font` 资源 id（跨设备/版本不稳定，需真机枚举映射）** |
| `fS` | 字号 |
| `fA` | 对齐方式 |
| `t` | 文字内容（含 `\n`） |
| `liS` / `leS` | 行距 / 字距 |
| `caps` | 是否强制大写 |

`tS[]`：`id / u / w / h / isP / a / isM`。`u` 形如 `s/1.webp`，**仍挂在 templates 前缀**：
`https://graphionica.fra1.cdn.digitaloceanspaces.com/t/s/1.webp` → 200（已验证，528B）。

---

## 5. 素材 URL 规则

```text
sticker   : <u>/<s[].u>              → .../s/1_flowers/1.webp        (200, 203KB)
sticker 预览: <u>/<s[].p>             → .../s/1_flowers/preview/1.webp (200, 11.7KB)
模板       : <templates.u>/<t[].p>    → .../t/1_base/1.webp            (200, 37.9KB)
模板小贴纸 : <templates.u>/<tS[].u>   → .../t/s/1.webp                 (200, 528B)
banner     : <home.u>/<b[].b>         → .../h/b/23.webp                (200, 174KB)
```

- 部分路径带 cache-buster：`10_home/preview.webp?v=4` → **落盘必须剥离 query**（Windows `?` 非法）
- 镜像 `u2`（`storage.yandexcloud.net`）在本机实测 **SSL connect error（curl exit 35）**，
  回退链只在境外环境才完整

## 6. 边界与错误行为（实测）

| 探测 | 结果 |
|------|------|
| `GET` 同一路径 | `404 Cannot GET /api/v2/home` |
| `version=-5` / `999999` | 200，`isV=true`，全量（服务端只做 `==` 比较） |
| `versionCode=999999` | 200，全量（不校验版本码） |
| `language` 300 字符垃圾 | 200，全量（不做 locale 白名单） |
| 缺 `language` / `country` | 200，全量 |
| `/api/v2/{version,settings,config,fonts,purchase,user,health,status,subscribe}` | 全部 404 |
| 顺序 12 次 `/stickers` | 12/12 200，647–3764ms，**无 429、无限速头** |

## 7. 客户端 premium 闸门（本 APK 为 MOD 版）

```java
// com.trimf.insta.d.m.s.S
public boolean isPremiumAndLocked() { return 4 == getDownloadStatus(); }
// getDownloadStatus(): 已订阅 -> 3（可下载）；未订阅且 isP=true -> 4（锁定）

// nh/b.java —— 本 APK 已被 patch 成恒真（原版应为 SKU/订阅校验）
public final boolean f() { return this.f13626b.size() > 0 ? true : true; }
public final boolean g() { return c() != null ? true : true; }
public final boolean h() { return d() != null ? true : true; }
```

即：**订阅状态只存在于客户端，服务端不参与校验**，所以 premium 素材的直链本身就是公开资源。

## 8. 第三方（非 Graphionica 目录）

`uc/c.java` → `https://messenger.stipop.io/v1/`（Stipop 贴纸 SDK）：

```text
GET  search/graphionica?q&userId&lang&countryCode&limit&pageNumber&animated
GET  search/keyword?userId&lang&countryCode&limit
POST analytics/send/{id}?userId&q&lang&countryCode
```

鉴权靠 `tc/d.java` 里的 OkHttp 拦截器写死 `apikey: <REDACTED>`（只对 Stipop 生效）。
