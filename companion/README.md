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

想让它开机自动跑：

```powershell
cd companion
.\install-autostart.ps1 -StartNow     # 注册登录自启并立刻启动
.\uninstall-autostart.ps1              # 取消
```

自启是**当前用户的计划任务**，不需要管理员权限。

---

## 2. 额度是怎么走到表盘上的

```
Codex CLI（本机已登录）
   │  ① http://127.0.0.1:8787/quota        本机快路径，毫秒级
   │     失败才落到 ↓
   │  ② codex app-server --listen stdio://  JSON-RPC 读周额度
   ▼
{"remaining_percent": 80, "reset_in_seconds": 92899}
   │  ③ BLE 写入（GATT 特征 7f0d4e66-…-5c02，handle 67）
   ▼
板子串口：ble: quota update remaining=80.0 reset=92899s
   ▼
360×360 表盘刷新
```

第 ① 步取的是**周窗口**（按 `windowDurationMins == 10080` 认，不按槽位名）。
所以周额度用尽时表盘显示 0% 是**正确数据**，不是 bug。

---

## 3. 为什么必须"一直挂着"

这是本项目额度问题的核心，也是这个文件夹存在的理由。

**现象**：空闲时每 **32.88 秒**断一次，额度同步不了。

**根因**：Windows 允许在空闲时把蓝牙网卡降功耗。没有 GATT 客户端挂着时，
网卡被降下去就不再发包，板子等满 32 秒的 supervision timeout 后拆链，
Windows 随即重连，如此循环（README 6.14）。

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
| 3 | 板子暴露额度服务吗（GATT 发现） | README 6.11（Windows GATT 缓存）、6.16（残留绑定） |
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
companion/
├── README.md                  ← 本文件
├── config.psd1                ← 唯一需要改的配置
├── start-companion.cmd        ← 双击：持续同步额度
├── start-companion.ps1          实现（也可以直接 pwsh 跑）
├── diagnose.ps1               ← 只读体检
├── repair-link.ps1            ← 链路修复（每步都要显式开关）
├── install-autostart.ps1      ← 注册登录自启
├── uninstall-autostart.ps1    ← 取消自启
├── menu.ps1 / menu.cmd        ← 所有相关程序的编号菜单
└── logs/                      ← 每次运行的日志（按天一个文件）
```

**程序本体不在这个文件夹里**，仍然在它们原本的位置
（`windows_companion.py` 在仓库根、诊断工具在 `tools/`）。
这是刻意的：复制一份就会出现两份会各自漂移的代码。
`menu.cmd` 把所有相关程序都列在一个菜单里，所以从这个文件夹一样能全部够到。

---

## 6. 配置（`config.psd1`）

全部可以留空，留空即自动。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `DeviceAddress` | 空 = 按名字自动发现 | **建议保持为空**，见下面的警告 |
| `IntervalSeconds` | 60 | 刷新间隔，`--watch` 不允许低于 10 |
| `WriteAttempts` | 4 | 每次刷新的 GATT 写入重试次数（README 6.11） |
| `WriteTimeoutMs` | 12000 | 单次写入超时；**必须远低于 30000**，否则板子会进 `ESP_GATT_CONGESTED`，之后通知永久失败 |
| `PythonPath` | 空 = 自动解析 | 依次找：配置 → 仓库 `.venv` → PATH → `py -3` → 托管解释器 |
| `CodexPath` | 空 = 自动找 | `codex.cmd` 不在 PATH 时填 |

> ⚠️ **不要把 `DeviceAddress` 写死。** 固件的广播地址是从出厂 MAC 派生 + 绑定代次
> 异或出来的，**每次换代次（重新烧录新固件）地址都会变**。
> 自动发现每次都从 `tools/ble_devices.py` 现读，天然免疫这件事。
>
> 另外：启动日志里 `base MAC set to …` 打印的是**基址**，不是广播地址
> （实测基址 `…:77` → 广播 `…:79`），**不要把那行抄进配置**。

---

## 7. 不要做的事

- **不要用 `serial_capture.py` 抓日志**：它打开串口会拉 DTR/RTS，可能顺手复位板子。
  用 `tools/serial_reset_capture.py`。
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
