# scripts/ — 脚本

本目录集中放**所有脚本**，与根目录的芯片烧录代码（`main/`、`CMakeLists.txt`、
`sdkconfig*`、`partitions.csv`）分开。四个子目录各管一件事，互不依赖。

> **解释器**：本机实测两个都装了 `pyserial`，差别只在 **Pillow**：
>
> | 解释器 | `pyserial` | `Pillow` | 用途 |
> | --- | --- | --- | --- |
> | `C:\Python312\python.exe` | ✅ | ✅ | **两个目录都能跑**（`companion\config.psd1` 里填的就是它） |
> | `D:\Espressif\python_env\idf5.4_py3.12_env\Scripts\python.exe` | ✅ | ❌ | 只能跑 `windows/`；跑 `assets/` 会 `No module named 'PIL'` |
>
> 所以统一用 `C:\Python312\python.exe` 最省事；`assets/` 下的脚本**必须**用它。

---

## build/ — ESP-IDF 环境与启动器

| 脚本 | 用途 |
| --- | --- |
| `idf_env.sh` | Git Bash 下 `source` 它：设置 `IDF_PATH` / `PATH` / `ESP_ROM_ELF_DIR`，并注册 `idf` 函数 |
| `idf_env.bat` | 同上，给 CMD / PowerShell 用 |
| `idf.bat` | CMD 一键入口：`.\scripts\build\idf.bat -p COM5 flash monitor` |
| `idf_runner.py` | 剔除 `MSYSTEM` / `MINGW_*` 后转交 `idf.py`（否则 idf.py 拒绝启动） |
| `idf_build.py` | Git Bash 下的另一条等价路径，顺带处理 MSYS 的 PATH 重写 |

```bash
# Git Bash
source scripts/build/idf_env.sh
idf build
idf -p COM5 flash monitor
```

## windows/ — Windows 端：BLE 诊断 / 额度同步 / 串口

### 先记住这一条

```bash
python scripts/windows/ble_enum_guard.py          # 只读，几秒；退出码 0 = 主机侧正常
python scripts/windows/ble_enum_guard.py --fix    # 不健康时切一次蓝牙无线电并轮询到恢复
```

**每次烧录完都跑一次。** `idf.py flash` 会打断 Windows 的 GATT 枚举，之后桌面端会
完全看不见设备；这个故障在固件里修不了，详见根目录 README 的「烧录固件之后」一节。

### 额度同步

| 脚本 | 用途 |
| --- | --- |
| `windows_companion.py` | 额度伴生程序：读本机 Codex 额度 → 写板子的私有 BLE 特征 |
| `test_windows_companion.py` | 单元测试（`CX_LIVE=1` 时额外跑真实 App Server / PS 桥） |

```bash
PY=C:/Python312/python.exe
$PY scripts/windows/windows_companion.py --json-only -v                 # 只读额度，不碰蓝牙
$PY scripts/windows/windows_companion.py --device-address <addr> --watch --interval 60 -v
$PY scripts/windows/windows_companion.py --device-address <addr> --repair-pairing -v
```

### BLE / 主机侧诊断

| 脚本 | 用途 |
| --- | --- |
| `ble_enum_guard.py` | **判 + 修**「主机枚举到哪一步了」；`--fix` 切无线电 |
| `ble_scan.py` | 列出 Windows 已知的 BLE 设备（约 30 秒） |
| `ble_devices.py` | 列出 BLE / 经典 / HID 设备与地址（认出对端是谁） |
| `ble_host_diag.py <addr>` | 读 PnP 节点 + 注册表绑定记录（只读，部分路径需 SYSTEM） |
| `bt_power_repair.py [--apply]` | 关掉网卡「允许关闭以省电」（`--apply` 需管理员） |
| `bt_radio_toggle.py [--status]` | 蓝牙无线电开 / 关。**会断开所有蓝牙设备几秒** |
| `bt_pair.py --list` | 命令行配对（`PairAsync` 从控制台进程走不通时的备选） |
| `bt_reconnect.py --address <addr> [--loop]` | 催 Windows 重连（开一个 GATT 会话把 ACL 拉起来） |
| `hid_caps.py [--write-test]` | 查这块板子的 HID 能力，应为 `UsagePage=0xFF00`、`input=64B output=64B` |
| `hid_rpc.py --method sys.version --listen 5` | 按桌面端的分帧直接发一条 RPC，绕开桌面端验协议 |
| `codex_host_state.py` | 桌面端进程 + 蓝牙网卡电源策略 |

### 串口

| 脚本 | 用途 |
| --- | --- |
| `boot_capture.py COM5 25` | **复位板子**并从第一行开始抓（不会漏开机横幅） |
| `serial_capture.py COM5 30` | 普通抓取。注意它会拉 DTR/RTS，**可能顺手复位板子** |
| `serial_reset_capture.py COM5 60` | 复位抓取的另一种写法 |

## assets/ — 图标 / 字体 / 背景 / 离线预览

固件只读 `main/` 下的生成产物，这些脚本负责生成它们。

| 脚本 | 产物 |
| --- | --- |
| `make_backgrounds.py` | `main/assets/bg_{day,night}.bin`（由 `preview/bg-*.png`） |
| `make_icon_textures.py` | `main/IconTextures.h`（6 个状态图标） |
| `make_battery_icons.py` | `main/BatteryIcons.h` + `battery_icon_data.py` |
| `make_pixel_font.py` | `main/PixelFonts.h`（由 `fonts/*.ttf`） |
| `make_icons_from_art.py` | **`main/IconMask.h` 的现役生成器**（描摹画师图标表） |
| `make_icons.py` | 早期的手绘图元版本，**已被上面那个取代**，产物同是 `main/IconMask.h` |
| `apply_ui_assets.py` | UI 换肤一键入口（**本地工具，不提交**），契约见 `debug/ui-replace.md` |
| `dial_preview.py` | 离线表盘渲染 + 文字溢出回归检查 |
| `pixel_preview.py` | 全部场景预览：`--scene all` |
| `test_battery_ui.py` | 电池图标分档的回归测试 |

> ⚠️ `make_icons.py` 和 `make_icons_from_art.py` 写的是**同一个** `main/IconMask.h`。
> 仓库里那份的抬头写着 `make_icons_from_art.py`，所以现役的是后者；
> 跑 `make_icons.py` 会覆盖它（`make_icons.py --check` 现在会报 out of date，
> 属预期，不是回归）。

生成器都支持 `--check`（只比对、不落盘），改完资产建议整套跑一遍：

```bash
PY=C:/Python312/python.exe
$PY scripts/assets/make_battery_icons.py --check
$PY scripts/assets/make_icons_from_art.py --check
$PY scripts/assets/make_icon_textures.py --check
$PY scripts/assets/make_pixel_font.py --check
$PY scripts/assets/make_backgrounds.py --check
$PY scripts/assets/test_battery_ui.py
$PY scripts/assets/pixel_preview.py --scene all
$PY scripts/assets/dial_preview.py
```

## companion/ — 额度伴生程序的一键菜单与登录自启

| 脚本 | 用途 |
| --- | --- |
| `menu.cmd` / `menu.ps1` | 交互式菜单，覆盖上面大部分命令 |
| `install-autostart.ps1` | 注册登录计划任务（**用当前用户身份，不要用管理员**） |
| `uninstall-autostart.ps1` | 注销该任务 |
| `start-companion.ps1` | 前台运行 `--watch`，崩了自动重启 |
| `stop-companion.ps1` / `status-companion.ps1` | 停止 / 查看状态 |
| `repair-link.ps1` | 一键走完「清主机记录 → 重新配对」 |
| `diagnose.ps1` | 环境自检（Python、地址、额度服务） |
| `config.psd1` | 地址 / 周期 / 重试 / 超时 |

需要 PowerShell 7。改过目录结构后要重跑 `install-autostart.ps1`，否则计划任务里
记的还是旧路径。
