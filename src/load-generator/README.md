# load-generator · 安全加固版（HTTP + 远程沙箱浏览器流量）

本目录是 [`opentelemetry-demo/src/load-generator`](https://github.com/open-telemetry/opentelemetry-demo/tree/main/src/load-generator) 的安全加固分叉，专为 chaos-otel-demo 在腾讯云 TKE 上的部署使用。

## 1. 加固背景与历史教训

upstream 的 `load-generator` 镜像把 Locust + Playwright + Chromium 全塞进一个容器，并以 root 运行。这种"All-in-one"在容器内运行 Chromium 会面临**多层结构性问题**：

| 层 | 问题 | 我们曾尝试的修复 | 结果 |
| --- | --- | --- | --- |
| 进程身份 | 默认 root 运行 | Dockerfile 加 `USER 10001` | ✅ |
| Chromium binary | Playwright 装的 `chrome-headless-shell` 强制注入 `--no-sandbox` ([#33566](https://github.com/microsoft/playwright/issues/33566)) | `playwright install --no-shell chromium` + `channel="chromium"` | ✅ |
| Playwright launch | Python SDK 默认仍注入 `--no-sandbox` ([#22822](https://github.com/microsoft/playwright/issues/22822)) | `chromium_sandbox=True` | ✅ |
| 容器 syscall | Docker 默认 seccomp 过滤 `unshare(CLONE_NEWUSER)`、`clone3` | 自定义 seccomp profile | ✅ |
| 容器 cap | `cap_drop:[ALL]` 让嵌套 user-ns 内拿不到 `SYS_CHROOT/SYS_ADMIN` | `cap_add: SYS_ADMIN, SYS_CHROOT` | ✅ |
| Chromium crashpad | 在严格 read-only rootfs + 限制 cap 下崩溃 | 没有干净的修复 | ❌ |

第 6 行是死结：每解决一层就冒出新一层，且解决方案越来越接近"放宽容器安全约束"的方向，与"安全加固"的初衷背道而驰。

我们也尝试过把 Chromium 抽离成独立 `chrome` Deployment、load-generator 通过 CDP（Chrome DevTools Protocol）远程连接的方案——它能跑通，但在自管 K8s 里运维和稳定性成本远高于带来的收益（多一个 Deployment、NetworkPolicy、seccomp installer DaemonSet、CDP 连接抖动重连逻辑）。

**结论**：在压测里用浏览器跑 E2E 流量是必要的（首页/购物车/RUM 真实链路只能在浏览器里跑出来），但浏览器**不该跑在 load-generator 容器里**。最干净的做法是把浏览器外推到**腾讯云 AGS (Agent Sandbox / e2b 兼容)** 提供的远程沙箱，本镜像里只保留 Locust + OTel SDK + Playwright client，不再 ship 任何 Chromium 二进制。

## 2. 当前形态

```
┌──────────────────────────────┐    HTTP/JSON     ┌──────────────────────┐
│  load-generator pod          │ ───────────────► │  frontend-proxy      │
│  Locust (HTTP + Playwright)  │                  │  (envoy → frontend)  │
│  uid=10001                   │                  └──────────────────────┘
│  read-only rootfs            │
│  cap_drop: ALL               │            CDP   ┌──────────────────────┐
│  RuntimeDefault seccomp      │ ◄──────────────► │  e2b sandbox         │
│  无 chromium / 无 setuid     │ HTTPS over WSS   │  (远端 Chromium)     │
└──────────────────────────────┘                  └──────────────────────┘
                                                          │  HTTP
                                                          └─► frontend-proxy
                                                              (沙箱 VPC 内访问
                                                               同 region 内网 CLB)
```

| 维度 | upstream | 本仓库（HTTP + 远程沙箱） |
| --- | --- | --- |
| Chromium 位置 | load-generator 容器内 | **腾讯云 AGS 远程沙箱** |
| 容器内打包 Chromium | ~1 GB binary | **完全没有** |
| 镜像大小 | ~1.3 GB（含 Chromium） | **~250 MB**（含 Playwright client） |
| capability | 默认 | **`cap_drop: [ALL]`** |
| seccomp | 默认 | **`RuntimeDefault`** |
| readOnlyRootFs | 否 | **是**（仅 `/tmp` tmpfs） |
| 容器内进程 | locust + chromium + zygote + renderer + ... | **仅 locust** |

容器内不再有任何 chromium / setuid / 浏览器相关进程。浏览器侧的攻击面（render/JIT/网络栈）整体外推到 e2b 沙箱里，沙箱由腾讯云 AGS 隔离运行、用完即销毁。

## 3. 目录结构

```
src/load-generator/
├── .dockerignore
├── Dockerfile               # 极简：python:3.12 + Locust + Playwright client (无 Chromium)
├── README.md                # 本文档
├── build-and-export.sh      # 离线构建 + 导出 tar.gz
├── locustfile.py            # WebsiteUser (HTTP) + WebsiteBrowserUser (走 e2b 沙箱)
├── people.json              # 合成测试用户数据（与 upstream 同步）
├── requirements.txt         # Python 依赖（含 e2b SDK）
└── run-local.sh             # 本地 venv debug 启动脚本
```

`mypy.ini` 是本地 lint 配置，已 `.gitignore`。

## 4. 浏览器流量的远程沙箱姿态

`locustfile.py` 里的 `WebsiteBrowserUser` 在每个 user 启动时通过 `Sandbox.create(template=ai-demo-browser-vpc)` 拉一个远程沙箱，然后用 Playwright `connect_over_cdp` 接入沙箱里的 Chromium。**关键取舍**：

- **沙箱 template 选 vpc 版本** (`ai-demo-browser-vpc`)：沙箱与你 K8s 集群同 VPC，能直接访问内网 CLB / Service ClusterIP，跨 region 公网访问受限是已知约束（沙箱出公网走 NAT，受 AGS 自身策略限制）。
- **沙箱出口 IP 散布腾讯云全国 EIP 池**：实测采样显示沙箱出口 IP 跨 15+ 个 `/16` 段，**无法做安全组白名单**。要么选 vpc 模板（同 VPC 内网通信），要么 demo 暴露层加 basic_auth 等业务侧鉴权（本仓库 chart 已有，详见 `charts/observable-stack/files/frontend-proxy/envoy.tmpl.yaml` 的 DIFF #4）。
- **沙箱内 Chromium 启动参数不可改**：沙箱由 envd / s6 拉起 chromium，启动 flags 已固化，`connect_over_cdp` 之后通过 CDP 也无法关闭 HTTPS-Upgrade 等浏览器策略。如果遇到"纯 IP + 无 HTTPS"的目标导致 HTTPS-Upgrade 死循环，要么换 vpc 模板（内网访问浏览器宽容），要么给 demo 上 HTTPS。
- **`page.goto(..., wait_until="commit")`**：沙箱里 Aegis SDK 的 `<script defer>` 会延迟 `domcontentloaded`，用 `commit`（HTTP 响应头到达即触发）+ `wait_for_selector` 显式等业务元素，避免任务无意义 60s 超时。
- **`on_stop` 主动 `sandbox.kill()`**：沙箱不会因 client 断开自动销毁，必须在 user stop 时显式 kill，避免漏单计费。

详细字段约束（env、CDP 路径等）见 `locustfile.py` 顶部 `WebsiteBrowserUser` 上方的设计注释。

## 5. 离线构建与分发（无远程仓库）

```bash
cd src/load-generator
IMAGE_TAG=2.0.4-secure-cdp-2 ./build-and-export.sh
# 产物:
#   _dist/otel-demo-load-generator_2.0.4-secure-cdp-2.tar.gz
#   _dist/otel-demo-load-generator_2.0.4-secure-cdp-2.tar.gz.sha256
```

把 `.tar.gz` 拷到节点后：

```bash
# 解压 → containerd 导入到 k8s.io namespace (kubelet 用的就是这个 namespace)
gunzip -c otel-demo-load-generator_<tag>.tar.gz | sudo ctr -n k8s.io images import -
sudo ctr -n k8s.io images ls -q | grep otel-demo-load-generator
```

`build-and-export.sh` 同时支持 `docker buildx`（默认）和 `docker build`（fallback），离线场景必须配合 `imageOverride.pullPolicy: IfNotPresent`，避免节点尝试拉取不存在的远程镜像。

## 6. 部署到 TKE

`charts/observable-stack/values-otel-demo.yaml` 已经写好。重点段：

```yaml
load-generator:
  envOverrides:
    - name: LOCUST_BROWSER_TRAFFIC_ENABLED
      value: "true"           # 打开浏览器流量 (WebsiteBrowserUser)
    - name: LOCUST_USERS
      value: "10"
    - name: LOCUST_SPAWN_RATE
      value: "1"
    - name: E2B_DOMAIN
      value: "your-e2b-domain.com"
    - name: E2B_TEMPLATE
      value: "ai-demo-browser-vpc"        # vpc 版本, 同 VPC 内网访问 demo
    - name: E2B_API_KEY
      valueFrom:
        secretKeyRef:
          name: e2b-api-key
          key: e2b-api-key
  imageOverride:
    repository: otel-demo-load-generator
    tag: "2.0.4-secure-cdp-2"
    pullPolicy: IfNotPresent
  securityContext:
    runAsNonRoot: true
    runAsUser: 10001
    allowPrivilegeEscalation: false
    readOnlyRootFilesystem: true
    capabilities: { drop: [ALL] }
  podSecurityContext:
    seccompProfile: { type: RuntimeDefault }
```

部署：

```bash
helm upgrade -n otel-demo observable-stack ./charts/observable-stack \
    -f charts/observable-stack/values-otel-demo.yaml
```

验证：

```bash
NS=otel-demo

# 1) load-generator 跑起来后看日志
kubectl -n "$NS" logs deploy/load-generator | \
    grep -E 'Instrumentation complete|Creating e2b browser sandbox|CDP connection established|Currency changed|Product added'

# 2) 安全自检：load-generator 容器内确认没有 chromium 进程
kubectl -n "$NS" exec deploy/load-generator -- \
    sh -c 'for p in /proc/[0-9]*; do
        cmd=$(tr "\0" " " < $p/cmdline 2>/dev/null)
        case "$cmd" in *chromium*|*chrome*) echo "!!! found: $cmd";; esac
    done; echo "scan done"'
# 期望: 仅输出 "scan done"

# 3) 看一个 e2b sandbox 实际拉起的痕迹 (在 AGS 控制台查也行)
kubectl -n "$NS" logs deploy/load-generator | grep "Sandbox LIVE_URL"
# 拷贝输出里的 URL 到本机浏览器, 能看到沙箱内 Chromium 实时画面 (noVNC)
```

## 7. 本地 debug

不上 K8s, 直接用 venv + e2b 沙箱跑一遍浏览器流量, 5 分钟回环：

```bash
cd src/load-generator

# 一键脚本: 创 venv (uv 优先) + 装依赖 + 设默认 env + 启动 Locust headless 模式
E2B_API_KEY=ark_xxxxxxxx \
E2B_TEMPLATE=ai-demo-browser-vpc \
LOCUST_HOST=http://your-cluster-host \
LOCUST_RUN_TIME=2m \
./run-local.sh
```

可选环境变量见 `run-local.sh` 顶部注释。期望日志：

```
INFO  [root] Creating e2b browser sandbox (template=ai-demo-browser-vpc)
INFO  [root] CDP connection established
INFO  [root] Sandbox LIVE_URL (open in browser): https://9000-...
INFO  [root] Currency changed to CHF
INFO  [root] Product added to cart successfully
INFO  [root] e2b sandbox killed
```

把 `Sandbox LIVE_URL` 那一整行拷到浏览器, 能直接看到沙箱里 Chromium 实时画面 + F12 DevTools, 是定位"任务超时"类问题的最快入口。

## 8. 与 upstream 同步

升级 upstream 后需同步：

1. `requirements.txt`：复制 [upstream requirements.txt](https://github.com/open-telemetry/opentelemetry-demo/blob/main/src/load-generator/requirements.txt)，**保留 `locust-plugins[playwright]`**，**保留 `e2b`** (本仓库新增)。
2. `people.json`：复制 [upstream people.json](https://github.com/open-telemetry/opentelemetry-demo/blob/main/src/load-generator/people.json)。
3. `locustfile.py` 的 `WebsiteUser` HTTP 任务集合按 upstream 同步；`WebsiteBrowserUser` 部分**不要**回退到 `chromium.launch()`——保持当前的 `_pwprep` 覆写 + e2b sandbox + `connect_over_cdp` 模式。每次改动后用 `./run-local.sh` 验证沙箱链路仍通。
