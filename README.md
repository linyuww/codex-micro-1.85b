# Codex Micro for Waveshare ESP32-S3-Touch-LCD-1.85B

把 Waveshare ESP32-S3-Touch-LCD-1.85B 变成 ChatGPT Desktop 的实体控制器。

## 预览

![Codex Micro 像素仪表盘](scripts/assets/preview/pixel-day.png)

## 功能

- 显示时间、日期、Codex 5 小时/周额度、智能体状态和电量
- 通过触摸选择智能体、发送消息和进行四向操作
- 通过 BOOT 键控制息屏、语音聊天、按住说话和蓝牙配对
- 支持 Wi-Fi 网页配网、自动校时和日夜主题
- 通过 BLE 连接 ChatGPT Desktop，并由 Windows 伴生程序同步额度

## 使用前准备

你需要：

- Waveshare ESP32-S3-Touch-LCD-1.85B
- Windows 10/11 与 ChatGPT Desktop
- ESP-IDF 5.4+（项目当前使用 v5.4.1）
- Python 3.10+

首次使用按下面顺序操作：

1. 根据本机安装位置修改 `scripts/build/idf_env.bat` 中的 ESP-IDF 路径。
2. 编译并烧录固件，将 `COM5` 换成开发板的实际串口：

   ```powershell
   .\scripts\build\idf.bat build
   .\scripts\build\idf.bat -p COM5 flash
   ```

3. 烧录后检查 Windows 是否完整识别设备；结果不是 `OK` 时执行修复：

   ```powershell
   python scripts/windows/ble_enum_guard.py
   python scripts/windows/ble_enum_guard.py --fix
   ```

4. 连接设备显示的 `CODEX-XXXX` 热点，打开 <http://192.168.4.1>，配置 2.4 GHz Wi-Fi。
5. 在 Windows“设置 → 蓝牙和设备 → 添加设备”中配对 **Codex Micro**，然后启动 ChatGPT Desktop。
6. 如需显示最新额度，双击 `scripts\companion\start-companion.cmd`。

> 如果电脑中保留了旧固件的 **Codex Micro** 记录，请先删除旧记录再重新配对。

## 操作方式

### 触摸屏

| 操作 | 功能 |
| --- | --- |
| 点按智能体图标 | 选择智能体 |
| 点按中央 Send | 发送 |
| 向上、下、左、右滑动 | 四向操作 |
| 长按 Send 6 秒 | 熄屏 |

### BOOT 键

| 操作 | 功能 |
| --- | --- |
| 单击 | 息屏/唤醒；亮屏时发送 |
| 双击 | Voice Chat |
| 按住 0.7 秒以上 | Push-to-talk，松开结束 |
| 按住 3 秒 | 清除绑定并进入蓝牙配对模式 |
| 按住 8 秒 | 更换蓝牙地址并重启，仅用于配对记录无法恢复时 |

屏幕熄灭时，第一次按键只负责唤醒。设备没有独立电源键，“熄屏”也不等于彻底断电。

## 更多文档

- [移植、实现与调试说明](docs/PORTING_AND_DEBUGGING.md)
- [脚本说明](scripts/README.md)
- [额度伴生程序](scripts/companion/README.md)

本项目基于 [digitsisyph/codex-micro-stopwatch](https://github.com/digitsisyph/codex-micro-stopwatch) 移植，沿用 MIT 许可。
