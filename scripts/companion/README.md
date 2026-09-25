# companion — 额度同步的启动文件夹

这个文件夹是**额度伴生程序的唯一入口**。所有脚本、配置、日志都在这里，
你要启动的东西只有三个：

| 想干什么 | 双击 |
| --- | --- |
| 让表盘一直显示最新额度（日常用这个） | `start-companion.cmd` |
| 出问题了，看看卡在哪一层 | `diagnose.ps1` 或 `menu.cmd` → 2 |
| 想把所有相关工具都列出来 | `menu.cmd` |

---

## 1. 快速开始

```
双击 companion\start-companion.cmd
```

窗口会停在前台，实时打印每次同步的日志。**按 Ctrl+C 停止**。
关掉窗口 = 停止同步（表盘会停在最后一次的数值上，重启后显示为 `STALE`）。

想让它开机自动跑、并且完全躲在后台：

```powershell
cd companion
.\install-autostart.ps1 -StartNow     # 注册登录自启并立刻启动
.\status-companion.ps1                # 确认在跑、没有可见窗口
.\stop-companion.ps1                  # 临时停掉（保留注册）
.\uninstall-autostart.ps1              # 彻底取消自启
```

自启是**当前用户的计划任务**，不需要管理员权限。细节见第 3 节。

---

## 2. 额度是怎么走到表盘上的

```
Codex CLI（本机已登录）
   │  ① http://127.0.0.1:8787/quota        本机快路径，毫秒级
   │     失败才落到 ↓
   │  ② codex app-server --listen stdio://  JSON-RPC 读 5h + 周额度
   ▼
{"five_hour_remaining_percent":80,"five_hour_reset_in_seconds":9289,
 "weekly_remaining_percent":62,"weekly_reset_in_seconds":92899}
   │  ③ BLE 写入（GATT 特征 7f0d4e66-…-5c02，handle 67）
   ▼
板子串口：ble: quota update 5h=80.0 reset=9289s weekly=62.0 reset=92899s
   ▼
360×360 表盘刷新
```

两个来源都会同时读取 300 分钟与 10080 分钟窗口，并按时长而不是槽位名识别。
中央显示 5 小时额度，屏幕圆边的分段进度环显示周额度。

---

## 3. 为什么必须"一直挂着"

这是本项目额度问题的核心，也是这个文件夹存在的理由。

**现象**：空闲时每 **32.88 秒**断一次，额度同步不了。

**根因**：Windows 允许在空闲时把蓝牙网卡降功耗。没有 GATT 客户端挂着时，
网卡被降下去就不再发包，板子等满 32 秒的 supervision timeout 后拆链，
Windows 随即重连，如此循环（debug/porting-log.md 6.14）。

**两个解法**：

1. **不需要管理员**：让伴生程序一直挂着。只要有 GATT 客户端在，网卡就不会被降功耗。
   —— 这正是 `start-companion.cmd` 干的事。
2. **需要管理员（更彻底）**：把网卡和板子 HID 节点的"允许计算机关闭此设备以节约电源"
   关掉。

```powershell
# 先看现状（不需要管理员）
.\repair-link.ps1

# 用管理员身份打开终端后
.\repair-link.ps1 -ApplyPower
```

实测（本机，2026-09-25）：

```
kept powered
    radio: Realtek Bluetooth Adapter
MAY BE POWERED DOWN
    board: Bluetooth Low Energy GATT compliant HID device
           BTHLEDevice\{00001812-…}_Dev_VID&02303a_PID&8360_…_288485b21c73
```

网卡那一项已经是"保持供电"，**板子的 HID 节点仍然允许断电** —— 所以第 2 步还没做完。

### 让它开机自启、常驻后台

```powershell
.\install-autostart.ps1 -StartNow
```

它注册一个**当前用户**的计划任务 `CodexMicroAllowanceCompanion`：登录后 30 秒启动，窗口隐藏。

| 参数 | 作用 |
| --- | --- |
| `-DelaySeconds 30` | 登录后延迟多少秒启动。蓝牙栈在刚登录时不一定就绪，早启动只是白烧重试 |
| `-StartNow` | 注册完立刻启动，不用注销重登 |
| `-Restart` | 已经在跑的话先停再起 |

**为什么是计划任务而不是 Windows 服务**：伴生程序靠 WinRT 蓝牙访问板子，并且读你本机的
Codex 登录态，两者都在**你的交互会话**里。服务跑在 session 0，那里够不到蓝牙设备，
结果会是"启动了但什么都同步不了"。

**为什么不会影响前台**：动作里带 `-WindowStyle Hidden`，任务本身也设成 Hidden，
再加 `-NonInteractive`（保证它永远不会停下来等你输入）。不抢焦点、不上任务栏。

验证：

```powershell
.\status-companion.ps1
```

正常时应该看到类似

```
[ok]   state=Running  logon delay=PT30S
[ok]   running in the background; nothing takes the foreground (2 process(es))
         launcher   pid 12345   no visible window
         companion  pid 23456   no visible window
```

（`no visible window` 是直接读 `MainWindowHandle == 0` 得出的，不是猜的。）

它每 60 秒才做一次 BLE 写入，其余时间在 sleep，CPU 占用可以忽略。

#### 换任务的坑：别用管理员身份注册

**实测**：如果 `install-autostart.ps1` 是从**管理员**终端跑的，生成的任务文件
（`C:\Windows\System32\Tasks\<任务名>`）属主会变成 `BUILTIN\Administrators`，
你的账号只拿到 `Read, Synchronize`。后果是：

- 非管理员终端**删不掉也改不了**它（`Unregister-ScheduledTask` 报"拒绝访问"）
- 连 `Register-ScheduledTask -Force` 覆盖也被拒

脚本会检测到这种情况并打印该跑的两条管理员命令，不会静默失败。所以：

> **注册自启用普通终端跑就够了**，不需要管理员。

#### 任务本身已经够用，改不动也不要紧

`start-companion.ps1` 每次尝试都会**重新按名字发现板子**，发现不到就等一会儿再试。
所以下面两件事都不需要动计划任务：

- 开机时蓝牙栈还没就绪 → 脚本自己重试到就绪为止
- 板子重新烧录换了广播地址 → 脚本下次尝试就用新地址

这也意味着：任务定义里那个 30 秒延迟、`Hidden` 标记、失败重启，都只是锦上添花。
**旧任务的动作指向的是同一个 `start-companion.ps1`，所以脚本层面的改进对它一样生效。**

**会不会和桌面端抢蓝牙？** 不抢连接，但共用同一条链路。桌面端正在密集读写 HID 时，
伴生程序的写入可能被 Windows 拒掉（`AccessDenied` / `ERROR_CANCELLED`），脚本会按
`WriteAttempts` 重试。这是 debug/porting-log.md 6.11 记录过的现象，属正常。

---

## 4. 出问题了按这个顺序查

```powershell
.\diagnose.ps1
```

它会按"从便宜到贵"的顺序逐层检查，并给出结论：

| 层 | 检查 | 坏了怎么办 |
| --- | --- | --- |
| 1 | 本机读得到额度吗（完全不碰蓝牙） | 确认 `codex` CLI 已登录；不在 PATH 就在 `config.psd1` 里写 `CodexPath` |
| 2 | Windows 看得见板子吗 | 设置 → 蓝牙和其他设备 → 添加设备 → 选 "Codex Micro" |
| 3 | 板子暴露额度服务吗（GATT 发现） | debug/porting-log.md 6.11（Windows GATT 缓存）、6.16（残留绑定） |
| 4 | 网卡/板子会不会被断电 | 见上面第 3 节 |

三个最典型的坏法，症状完全不同：

| 症状 | 卡在哪 | 怎么修 |
| --- | --- | --- |
| 板子有 `pairing complete`，桌面端日志刷 `0x00000057` | 链路没加密（绑定残留） | 复位板子；固件的 `clearIncompatibleBondsOnce()` 会自动清一次 |
| 板子串口**一条 `host connected` 都没有** | 主机侧根本没发起连接 | `.\repair-link.ps1 -ToggleRadio` |
| 能连上、握手也过，但表盘不动 | 桌面端没在发 RPC | 切一次蓝牙无线电，或重启桌面端 |

---

## 5. 目录里有什么

```
scripts/companion/
├── README.md                  ← 本文件
├── config.psd1                ← 唯一需要改的配置
├── start-companion.cmd        ← 双击：持续同步额度（前台窗口）
├── start-companion.ps1          实现（也可以直接 pwsh 跑）
├── status-companion.ps1       ← 看后台是否在跑（任务 / 进程 / 日志）
├── stop-companion.ps1         ← 停掉后台（保留自启注册）
├── diagnose.ps1               ← 只读体检
├── repair-link.ps1            ← 链路修复（每步都要显式开关）
├── install-autostart.ps1      ← 注册登录自启（隐藏窗口）
├── uninstall-autostart.ps1    ← 取消自启
└── menu.ps1 / menu.cmd        ← 所有相关程序的编号菜单
```

**程序本体不在这个文件夹里**，仍然在它们原本的位置
（`scripts/windows/windows_companion.py` 与同目录的诊断工具）。
这是刻意的：复制一份就会出现两份会各自漂移的代码。
`menu.cmd` 把所有相关程序都列在一个菜单里，所以从这个文件夹一样能全部够到。

---

## 6. 配置（`config.psd1`）

全部可以留空，留空即自动。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `DeviceAddress` | 空 = 按名字自动发现 | **建议保持为空**，见下面的警告 |
| `IntervalSeconds` | 60 | 刷新间隔，`--watch` 不允许低于 10 |
| `WriteAttempts` | 4 | 每次刷新的 GATT 写入重试次数（debug/porting-log.md 6.11） |
| `WriteTimeoutMs` | 12000 | 单次写入超时；**必须远低于 30000**，否则板子会进 `ESP_GATT_CONGESTED`，之后通知永久失败 |
| `PythonPath` | 空 = 自动解析 | 依次找：配置 → 仓库 `.venv` → PATH → `py -3` → 托管解释器 |
| `CodexPath` | 空 = 自动找 | `codex.cmd` 不在 PATH 时填 |

> ⚠️ **不要把 `DeviceAddress` 写死。** 固件的广播地址是从出厂 MAC 派生 + 绑定代次
> 异或出来的，**每次换代次（重新烧录新固件）地址都会变**。
> 自动发现每次都从 `scripts/windows/ble_devices.py` 现读，天然免疫这件事。
>
> 另外：启动日志里 `base MAC set to …` 打印的是**基址**，不是广播地址
> （实测基址 `…:77` → 广播 `…:79`），**不要把那行抄进配置**。

---

## 7. 不要做的事

- **不要用 `serial_capture.py` 抓日志**：它打开串口会拉 DTR/RTS，可能顺手复位板子。日志统一落在仓库根的 `logs/companion/`。
  用 `scripts/windows/serial_reset_capture.py`（或 `boot_capture.py`）。
- **不要为了修额度去擦整片 Flash 或改蓝牙地址**。先按第 4 节的顺序走。
- **不要在 `WriteTimeoutMs` 上调到 30000 以上**，见上表。
- **不要指望脚本替你完成首次配对**：`PairAsync()` 从普通控制台进程调用会直接返回
  `Failed`，此时板子串口连一条 `host connected` 都不会出现。首次配对只能用系统设置。

---

## 8. 一次性清板子那半的绑定（最后手段）

只有当主机侧和板子侧的绑定两半都对不上、且上面的步骤都无效时才用。
**需要 COM 口和 esptool，会清掉板子 NVS 里的绑定区（0x9000，长度 0x4000）**：

```bash
python -m esptool --chip esp32s3 -p COM5 erase_region 0x9000 0x4000
```

这一步没有做成脚本，因为它会不可逆地清掉板子的配对状态，必须你手动确认端口号。
