# XCAppStore 本地兼容服务

这是根据 `com.ecarx.appstore` 1.2.19081110.624 的静态分析制作的最小兼容服务。它只依赖 Python 标准库，不修改原 APK。

## 1. 注册一个 APK

需要 Android SDK Build Tools 中的 `aapt`。本机已经检测到可用版本。

```bash
cd /Users/pengcheng/Documents/Codex/2026-09-02/ji/outputs/xcappstore-local-server
python3 register_apk.py /绝对路径/your-app.apk --name "我的应用"
```

脚本会读取真实包名、`versionCode` 和 `versionName`，复制 APK 到 `apks/`，并更新 `data/apps.json`。重复注册同一包名会更新目录记录；旧版本文件不会自动删除。

## 2. 本机验证

```bash
python3 server.py --host 127.0.0.1 --port 8080 \
  --public-base-url http://127.0.0.1:8080
```

另一个终端执行：

```bash
curl http://127.0.0.1:8080/healthz
curl http://127.0.0.1:8080/api/v1/product
```

## 3. 给车机使用

应用把 API 地址写死为 `http://api.xchanger.cn/api/v1/`，所以实际使用时要同时满足：

1. 车机和这台 Mac 位于同一个隔离的局域网；
2. 车机 DNS 将 `api.xchanger.cn` 解析为 Mac 的局域网 IP；
3. Mac 的 TCP 80 端口把请求转给本服务的 8080 端口；
4. macOS 防火墙允许来自该隔离局域网的连接。

启动 Python 服务：

```bash
python3 server.py --host 0.0.0.0 --port 8080 \
  --public-base-url http://api.xchanger.cn
```

本机已有 Docker 和 `nginx:1.27.4-alpine` 镜像，可用非特权 Python 进程配合容器占用 80 端口：

```bash
docker run --rm --name xcappstore-proxy \
  -p 80:80 \
  -v "$PWD/nginx.conf:/etc/nginx/nginx.conf:ro" \
  nginx:1.27.4-alpine
```

在你控制的 DNS 服务器或路由器里添加：

```text
api.xchanger.cn  ->  <Mac 的局域网 IP>
```

`dnsmasq.conf.example` 给出了 dnsmasq 的写法。不要修改公共 DNS；只对你自己的车机测试网络启用该覆盖。

## 4. 验证顺序

先在同一 Wi-Fi 的另一台设备上验证：

```bash
nslookup api.xchanger.cn
curl http://api.xchanger.cn/healthz
curl http://api.xchanger.cn/api/v1/product
```

确认解析结果是 Mac 的局域网 IP，并且目录中出现已注册应用后，再打开车机应用商店。服务终端应出现 `/api/v1/...` 请求日志。点击安装后还应出现 `/files/...apk` 请求。

## 5. 重要限制

- 只支持单个 APK，不支持 `.apks`、`.xapk` 或 split APK 集合。
- APK 必须兼容车机的 Android 版本和 CPU ABI。
- 覆盖已安装应用时，新 APK 必须使用与旧应用相同的签名，并提高 `versionCode`。
- 静默安装依赖原应用商店仍是系统应用/特权应用，并实际持有 `INSTALL_PACKAGES` 权限。把这个 APK 当普通用户应用重新安装，通常不会获得该权限。
- 应用商店不会校验下载文件的哈希或服务端签名。DNS 覆盖仅应存在于隔离测试网络，完成后立即撤销。

## 6. 运行测试

```bash
python3 -m unittest discover -s tests -v
```

## 7. Docker 部署

应用服务镜像使用仓库根目录的 `Dockerfile` 构建。运行时挂载目录和 APK，避免把 APK 烘焙进镜像：

```bash
docker build -t xchanger-server:local .
docker run -d --name xchanger-server --restart unless-stopped \
  -p 80:8080 \
  -v "$PWD/data:/app/data:ro" \
  -v "$PWD/apks:/app/apks:ro" \
  xchanger-server:local
```

`deploy/Corefile` 用于 CoreDNS：它覆盖 `api.xchanger.cn`，其余记录转发至公共 DNS。必须通过 `DNS_ALLOWED_CIDR` 限制可使用递归解析的客户端来源，避免成为公网开放解析器。

```bash
docker run -d --name xchanger-dns --restart unless-stopped \
  --read-only --cap-drop ALL --cap-add NET_BIND_SERVICE \
  --security-opt no-new-privileges \
  -e DNS_ALLOWED_CIDR='<测试网络公网出口 IP>/32' \
  -p '<服务器内网 IP>:53:53/udp' \
  -p '<服务器内网 IP>:53:53/tcp' \
  -v "$PWD/deploy/Corefile:/Corefile:ro" \
  coredns/coredns:1.11.3 -conf /Corefile
```
