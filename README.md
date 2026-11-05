# Android USB Rescue

一个用于 Android 黑屏恢复辅助的 Windows 桌面工具：将鼠标操作映射到手机参考图，通过 USB AOA HID 发送输入，并在手机已开放 MTP 文件传输后取回截图，逐步确认操作结果。

由一次 Redmi K60 恢复案例整理而来。**参考图与取回的截图都是静态图片，不是实时投屏。** 其他机型是否支持相同输入方式，需要单独验证。

## 这版做了什么

- 默认整张等比显示，保留原图像素模式、网格及坐标换算。
- 支持选择自己的竖屏参考图；没有图片也能正常启动。
- 一键“截图并刷新”，也可单独截图、检查文件通道。
- 截图目录变化或句柄短暂失效时，完整重新扫描一次；持续错误会保留原画面并显示原因。
- 操作过程中移出图片、失去焦点、按 Esc 或超时，都会释放触点。
- 连接参数可配置；必须指定准确设备序列号，默认不自动连接。
- 运行数据保存在用户数据目录，不写入源代码目录。
- 可选的本机文件接口支持读取状态、单次截图及文件检查；默认关闭，不开放网络端口。

项目没有附带个人截图、设备历史、密钥、驱动程序或第三方 DLL。

## 安装与启动

主要验证环境是 Windows，要求 **Python 3.10 或更新版本，含 tkinter**。Python 官方 Windows 安装包通常自带 tkinter。

下载源码后双击 `install.cmd` 安装，再双击 `start.cmd`。启动脚本会建立本项目的 `.venv`，通过 PyPI 安装 Pillow 和 libusb-package。

也可以手动安装：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\python.exe -m android_usb_rescue
```

仅查看界面、加载参考图，不访问手机：

```powershell
.\.venv\Scripts\python.exe -m android_usb_rescue --dry-run
```

查看全部启动参数：

```powershell
.\.venv\Scripts\python.exe -m android_usb_rescue --help
```

## 连接前提

1. 使用支持数据传输的 USB 线。
2. 手机支持 AOA 2 HID；电脑能够通过 libusb 访问目标设备。
3. Windows 对应接口需要可用的 WinUSB 驱动。这与普通文件传输/ADB 的驱动用途不同。
4. 回读截图需要手机端已经开放 MTP 存储；仅显示“已连接”不代表文件访问已授权。
5. 使用其他投屏软件前关闭本工具，避免争用 USB。

本工具只使用现有 USB 接口，不安装驱动、不切换 accessory 模式、不执行 USB reset。驱动操作应由使用者确认目标接口后完成，并保留原驱动信息；不要替换其他外设或 USB 复合设备父节点。

## 使用流程

1. 选择一张布局合适的竖屏手机参考图。
2. 填写手机序列号并点击连接；需要其他 VID/PID 时，使用启动参数 `--vid` / `--pid` 指定。
3. 确认图片来源后启用鼠标控制。左键点击发送点按，按住左键拖动发送滑动。
4. 手机端文件传输可用后，点击“截图并刷新”，等待新截图显示。
5. 刷新期间会暂停触摸，完成后按界面提示重新启用控制。
6. 开启并授权 USB 调试后，可以关闭本工具，改用正常的 ADB/scrcpy 投屏。

切换参考图只改变电脑显示，不会自动切换手机页面。鼠标滚轮只滚动电脑参考图。受保护页面、未唤醒的手机或未开放的存储可能无法生成/读取新截图，请看图像时间和底部结果。

可以通过启动参数指定目标，以下均为示例值：

```powershell
.\.venv\Scripts\python.exe -m android_usb_rescue --serial YOUR_DEVICE_SERIAL --vid 2717 --pid ff40
```

默认 VID/PID 只是已验证案例使用过的组合，不能据此选择其他手机；序列号必须填写自己的。连接时会再次精确核对，不符合目标时不发送 HID 输入。

## 本地数据与图片清单

Windows 默认位置：`%LOCALAPPDATA%\AndroidUsbRescue`。

- `config.json`：本机连接与界面设置。
- `status.json`：当前状态。
- `references/`：自己的参考图片。
- `screenshots/`：从手机取回的截图。
- `references.json`：可选的图片清单。

使用 `--data-dir PATH` 或环境变量 `ANDROID_USB_RESCUE_HOME` 可指定数据目录。文件可能包含设备标识和个人画面，应留在自己的电脑上。仓库忽略运行数据、图片、日志和凭据文件。

标准图片清单示例见 [references.example.json](examples/references.example.json)。清单中的图片必须位于清单所在目录内；横屏、损坏或越界文件会被跳过。

## 可选本机接口

仅在需要脚本协作时使用 `--enable-local-api` 启用。支持 `status`、`capture_screenshot`、`check_files` 三个动作；请求需要匹配当前进程、有效时间和单次请求 ID。该接口不接受任意键盘文字、密码或鼠标坐标。

```powershell
.\.venv\Scripts\android-usb-rescue-api.exe status
.\.venv\Scripts\android-usb-rescue-api.exe capture_screenshot
# 轮询 status，等 busy=false 后再检查文件
.\.venv\Scripts\android-usb-rescue-api.exe check_files
```

指定了其他数据目录时，为客户端使用相同的 `--data-dir`。返回 `accepted=false` 表示操作没有开始，需要等当前操作结束；不要当作取图成功。只有状态中的 `last_file_check.ok=true` 且截图时间已核对后，才表示成功取回图片。

## 开发与验证

```powershell
.\.venv\Scripts\python.exe -m pip install ".[dev]"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m build
```

自动测试使用模拟 USB，不会读取或控制真实手机。硬件恢复流程曾在 Redmi K60 / HyperOS 3 上验证；重构版本的自动测试不能代替其他机型的硬件测试。

代码结构与边界见 [开发说明](docs/development.md)，依赖见 [第三方说明](THIRD_PARTY_NOTICES.md)。
