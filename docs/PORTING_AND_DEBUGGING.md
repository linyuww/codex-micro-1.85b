# 移植、实现与调试说明

本文收纳根目录 README 中不影响日常使用的技术细节，包括硬件适配、协议、构建配置、移植限制与排障记录。普通用户只需阅读 [README](../README.md)。

## 硬件适配

| 项目 | 原版 C152 StopWatch | Waveshare 1.85B | 处理方式 |
| --- | --- | --- | --- |
| SoC | ESP32-S3R8 | ESP32-S3R8 | 保持 240 MHz、8 MB PSRAM、16 MB Flash |
| 屏幕 | 466×466 CO5300 AMOLED | 360×360 ST77916 QSPI | 重新布局为 360×360 |
| 触摸 | CST9217 | CST816S | 使用 ESP-IDF CST816S 组件 |
| 按键 | 三个按键 | BOOT / GPIO0 | 改为单键多手势 |
| 电源 | M5PM1 PMIC | BQ27220 电量计 | 只能检测电量，不能切断电源轨 |
| 音频 | 内置喇叭 | ES8311 + I2S | 使用寄存器级 ES8311 驱动 |
| 震动 | 有 | 无 | 不支持触觉反馈 |

主要引脚定义集中在 `main/board_config.h`，UI 布局位于 `main/dashboard_ui.h`。

## 固件实现

- ST77916 使用 QSPI、RGB565 与 PSRAM 帧缓冲，按条带推送到 360×360 屏幕。
- CST816S、ES8311 与 BQ27220 共享 I2C 总线。
- Wi-Fi 负责首次网页配网与 SNTP 校时，凭据只保存到设备 NVS。
- BLE 使用 Bluedroid GATTS/GAP，提供 HID、Battery、Device Information 和私有额度服务。
- Windows 伴生程序读取本机 Codex 登录态中的 5 小时/周额度，再写入私有 GATT 特征；不需要 OpenAI API Key。

## BLE 协议

| 服务 | UUID / 标识 | 用途 |
| --- | --- | --- |
| Device Information | `0x180A` | 制造商和 PnP 信息 |
| HID over GATT | `0x1812` | ChatGPT Desktop 控制与 JSON-RPC |
| Battery Service | `0x180F` | 电量通知 |
| 私有额度服务 | `7f0d4e66-2ac2-4a71-bfbe-4ef61a0e5c01` | 写入额度快照 |

HID output report 使用 63 字节报告体：首字节为 `0x02`，第二字节是当前分片长度，后续最多 61 字节为以换行结尾的 JSON-RPC 数据。

主要出站方法：

- `v.oai.hid`：Send、Voice Chat、push-to-talk 与智能体选择
- `v.oai.rad`：四向滑动

BLE 特征默认要求加密访问。主机和设备各保存一半配对状态，擦除 NVS、替换固件或更换蓝牙地址后可能需要重新配对。

## 构建配置

Windows：

```powershell
.\scripts\build\idf.bat build
.\scripts\build\idf.bat -p COM5 flash monitor
```

Git Bash：

```bash
source scripts/build/idf_env.sh
idf build
idf -p COM5 flash monitor
```

如果修改了 `sdkconfig.defaults`，应删除已有 `sdkconfig` 后重新生成，否则旧配置会覆盖 defaults。

关键配置：

| 配置 | 值 | 原因 |
| --- | --- | --- |
| `CONFIG_ESP_MAIN_TASK_AFFINITY_CPU1` | `y` | 主任务与蓝牙栈分核 |
| `CONFIG_ESP_MAIN_TASK_STACK_SIZE` | `12288` | 满足全屏绘制的栈需求 |
| `CONFIG_BT_CTRL_LE_PING_EN` | `n` | 避免认证载荷超时引起断链 |
| `CONFIG_BT_SMP_MAX_BONDS` | `8` | 在 NVS 保存配对信息 |
| `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG` | `y` | 使用原生 USB-C 串口日志 |

## 已知限制

- 没有震动马达，不能提供原版的触觉反馈。
- 开发板没有可控 PMIC，“关机”只是关闭背光的空闲态。
- GPIO0 同时是启动模式 strapping 引脚，因此不使用 deep sleep 唤醒。
- 已连接时设备不继续广播，ChatGPT Desktop 占用链路时，第二个 BLE 中心设备可能无法写入额度。
- Windows 可能缓存不完整的 GATT 枚举；这是主机侧状态，重复烧录固件无法修复。

## 排障顺序

### 烧录后桌面端没有反应

先运行只读检查：

```powershell
python scripts/windows/ble_enum_guard.py
```

如果结果为 `partial` 或 `never-enumerated`：

```powershell
python scripts/windows/ble_enum_guard.py --fix
```

修复会短暂切换 Windows 蓝牙无线电，耳机、鼠标等设备会暂时断开。

### 已连接但显示“功能受限”

1. 先确认 `ble_enum_guard.py` 是否为 `OK`。
2. 若串口显示 `host connected` 且 `bonds=1`，重启 ChatGPT Desktop。
3. 若显示 `bonds=0`，删除 Windows 中的旧设备记录，再长按 BOOT 3 秒重新配对。
4. 只有主机记录确实无法恢复时，才长按 BOOT 8 秒更换蓝牙地址。

不要因为这个症状直接清空整片 Flash、关闭 BLE 加密或频繁修改绑定版本；这些操作会把主机侧缓存问题变成新的配对问题。

### 额度不更新

按层排查：

```powershell
python scripts/windows/windows_companion.py --json-only -v
python scripts/windows/ble_scan.py
python scripts/windows/windows_companion.py --device-address xx:xx:xx:xx:xx:xx --probe-only -v
python scripts/windows/windows_companion.py --device-address xx:xx:xx:xx:xx:xx --once -v
```

依次确认额度读取、设备地址、服务发现和写入。日常运行建议直接使用 `scripts\companion\start-companion.cmd`。

## 调试工具与记录

- `scripts/windows/`：BLE 枚举、配对、HID、额度和串口诊断工具
- `scripts/assets/`：图片资产生成与离线 UI 预览
- `scripts/assets/pixel_preview.py --scene all`：生成全部预览场景
- `debug/session-2026-09-25.md`：一次完整的“已连接但功能受限”定位记录
- `debug/reference/devcomm.js`：用于核对桌面端 HID 分帧的参考产物

本地 `debug/` 和 `logs/` 目录可能包含串口号、蓝牙地址与用户名路径，默认被 `.gitignore` 排除。需要保留调试资料时，应先清理隐私信息再提交。

## 代码入口

| 文件 | 作用 |
| --- | --- |
| `main/main.cpp` | 应用状态机、手势、电源策略与主循环 |
| `main/codex_ble.cpp` | BLE 服务与 JSON-RPC 收发 |
| `main/dashboard_ui.h` | 仪表盘布局和绘制 |
| `main/display.cpp` | ST77916 初始化与推屏 |
| `main/touch_input.cpp` | CST816S 触摸输入 |
| `main/battery.cpp` | BQ27220 电量读取 |
| `main/chime.cpp` | ES8311 完成提示音 |
