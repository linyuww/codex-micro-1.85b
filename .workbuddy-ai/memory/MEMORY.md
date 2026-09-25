# Project memory — codex-micro-1.85b（worktree: codex-fix-ble-codex）

## 工具链

- 构建/烧录：`source ./idf_env.sh && idf build` / `idf -p COM5 flash`。
  从零编译约 12 分钟；改了 `sdkconfig.defaults` 必须先删 `sdkconfig` 再编译。
- Python 用 ESP-IDF 自带的：`D:\Espressif\python_env\idf5.4_py3.12_env\Scripts\python.exe`
  （系统 Python 没有 pyserial）。
- `managed_components/` 从 `C:\Users\86147\Documents\Codex\2026-09-16\...\work\codex-micro-1.85b`
  复制过来，避免联网拉组件。该目录同时保留了原始工作副本与 build 缓存。
- 抓串口：`boot_capture.py COM5 <秒>`（会先复位，能抓到开机第一行）。
  `tools/serial_capture.py` 会拉 DTR/RTS，可能顺手复位板子。

## BLE 不变式（改动前必读 README 6.17）

- **绑定（bond）是这条链路最脆的一环。** 板子这半和主机那半任一侧被清掉，
  表现不是「连不上」，而是「连上了但 Codex 显示功能受限，约 10 分钟后才好」。
- **不要**在 `ESP_GATTS_CONNECT_EVT` 里调 `esp_ble_set_encryption()`。
  实测：绑定不一致时 16 ms 一轮断连循环，比不调用更糟；绑定健康时也不需要。
- **不要**动 `CODEX_BLE_BOND_REVISION`（默认 0 = 不清绑定）。每次加一都会毁掉
  用户已配好的绑定，这正是「重启后又失效」的来源。
- **不要**为了绕过坏记录改 `kBondGeneration`，除非 `--repair-pairing` 也救不回来。
  改地址 = Windows 认为板子从没出现过，之后必须手动配对。
- 连接日志里的 `bonds=N` 是判据：`bonds=0` 而主机在连 = 绑定两半不一致。
- **另一种坏法**：主机每轮只读电池(63/64/65)和配额(68)、从不读 HID 服务(55–62)，
  且 `hid_caps.py` 报 no HID interface、PnP 里没有 `BTHLE\DEV_<addr>` 节点。
  这条地址已经修不回来，只能换 `kBondGeneration`。
- **被连接期间板子不广播**（`startAdvertising()` 在 `conns>0` 时直接返回），
  所以主机一旦自动连上，Windows「添加设备」就扫不到它 —— 这是
  「电脑蓝牙搜索不到 Codex Micro」的原因，不是射频问题。
- 启动日志里的 `base MAC` 是基址，**广播地址 = 基址 + 2**，脚本里不要直接抄。
  当前 `gen=0x5F` → 基址 `…:1c:73` → 广播 `…:1c:75`。

## 主机侧工具

- `windows_companion.py --repair-pairing`：`Unpaired` 那一步是有效的（连 LTK 一起删）；
  `pair=Failed` 是预期行为，控制台进程弹不出 ConfirmOnly 确认框。
- `tools/ble_host_diag.py <addr>`：读 PnP 节点 + 配对状态。
- `tools/hid_caps.py`：HID 能力自检（应为 `UsagePage=0xFF00`、input/output 64 B）。
- `tools/bt_radio_toggle.py`：治 Windows 蓝牙栈假死（不需要管理员）。
- 本机 `127.0.0.1:8787` 有 `codex-ornament-bridge.exe` 提供 `/quota`，
  是 `windows_companion.py` 的首选额度来源。

## 用户约定

- 所有命令用 PowerShell 7：`C:\Program Files\PowerShell\7\pwsh.exe`。
- UI/资产相关的 skill 与工具不入库（见原工作目录的 MEMORY.md）。
