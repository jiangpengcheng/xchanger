# XCAppStore.apk 静态分析报告

## 结论

方案在协议层面可行，而且无需修改或重签应用商店 APK：应用将 API 固定为明文 HTTP 地址 `http://api.xchanger.cn/api/v1/`，允许 cleartext 流量，没有发现应用层请求签名、证书锁定或下载文件哈希校验。把该域名在受控局域网内解析到本地兼容服务，即可让商店展示并下载服务端提供的 APK。

真正的不确定点是车机安装权限，而不是网络协议。如果原应用商店仍作为系统/特权应用安装，它声明的 `android.permission.INSTALL_PACKAGES` 可能允许静默安装；若它只是后来以普通用户应用方式安装，下载能够成功，但安装通常会被 Package Manager 拒绝。

## 样本信息

- 包名：`com.ecarx.appstore`
- 版本：`1.2.19081110.624`，versionCode `624`
- minSdk：18；targetSdk：28
- SHA-256：`fa8a2810bd60599ee18f78a37277183b7416e6bad2675c21eb58349ea3669dcc`
- 签名主体：ECarX Android；样本使用旧式 SHA1withRSA 签名
- 网络库：Retrofit 2、OkHttp 3、RxJava 2
- 下载库：FileDownloader + OkHttp

样本内存在一个看起来用于开发测试的旧 JWT 和测试账号资料，但生产配置路径实例化的是空的 `ConfigCompatDefault`，不是该 Fake 实现。本项目没有复制或使用这些敏感值。

## 服务端与接口

固定 Base URL：

```text
http://api.xchanger.cn/api/v1/
```

接口：

| 方法 | 路径 | 响应模型 |
|---|---|---|
| GET | `banner/index` | `{ "banners": [...] }` |
| GET | `product/special` | `{ "specials": [...] }` |
| GET | `product/special/{alias}` | 产品列表 |
| GET | `product/catalog` | `{ "products": [分类...] }` |
| GET | `product/history?type=hot` | `{ "histories": [...] }` |
| GET | `product?search=...` | 产品列表 |
| GET | `product?categoryId=...` | 产品列表 |
| GET | `product/{pid}` | 产品详情 |
| GET | `app/version?packages=...` | `{ "apps": [...] }` |

每个请求会增加这些头：`X-CLIENT-ID`、`X-ENV-TYPE`、`X-APP-ID`、`X-DEVICE-MODEL`、`X-VEHICLE-MODEL`、`X-STORE: APP`、`X-OPERATOR-CODE: LYNKCO`、系统和 SDK 版本头；登录后才会增加 `Authorization`。本地服务不需要信任或保存这些标识。

原线上接口在 2026-09-03 仍可响应，匿名访问 `product/catalog` 和 `product/{pid}` 返回 200。线上服务还同时接受 HTTP 与 HTTPS；但这个 APK 明确使用 HTTP。

## APK 下载与安装链路

1. 产品 JSON 的 `attributeGroups.AppSrc` 被直接映射为下载 URL。
2. `AppPackageName`、`AppVersionCode` 和 `AppSize` 被放入 `RemoteApkBean`。
3. FileDownloader 将文件保存为 `<package>_build_<versionCode>.apk`。
4. 下载完成立即创建安装任务。
5. Android 9 及以上使用 `PackageInstaller.Session`；更旧系统执行 `pm install -r ...`。
6. 代码会解析 APK 的真实包名并与 JSON 包名比较，但该检查发生在安装调用之后，而且不阻止调用。

没有发现：

- 对 `AppSrc` 域名的白名单；
- APK 的 SHA-256/MD5 校验；
- 商店自定义的 APK 签名允许列表；
- API 请求 HMAC/nonce 签名；
- 本应用网络代码中的 TLS pinning。

Android Package Manager 自身仍会执行正常的 APK 签名、版本、SDK、ABI 和权限校验。

## 推荐拓扑

```text
车机应用商店
  -> DNS 查询 api.xchanger.cn
  -> 受控 DNS 返回 Mac 的局域网 IP
  -> Mac:80 (nginx 容器)
  -> Mac:8080 (server.py)
  -> JSON 返回 http://api.xchanger.cn/files/<apk>
  -> 车机下载并请求系统安装
```

只需覆盖 `api.xchanger.cn`；兼容服务返回的 APK 地址也使用该域名，因此不需要同时劫持原静态资源域名。

## 风险与回退

- 这是一个具有系统安装能力的旧客户端，明文 HTTP 设计本身存在严重供应链风险。必须使用隔离 Wi-Fi/热点，不能在日常网络长期保留 DNS 覆盖。
- 不要代理或记录 `Authorization`、VIN、IHUID 等请求头。本实现完全忽略它们。
- 先用无害、自签名、最小权限的测试 APK 验证，不要先替换系统组件。
- 若安装失败，优先收集商店日志中的 `INSTALL_FAILED_*`，它能直接区分签名冲突、SDK/ABI 不兼容、权限不足和空间不足。
- 回退只需撤销 DNS 覆盖并停止本地 HTTP 服务，原 APK 未被修改。
