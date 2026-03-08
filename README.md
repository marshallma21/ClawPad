# ClawPad - iOS 设备控制器

在 macOS 上通过 USB 连接 iOS 设备（iPhone / iPad），实现 **截图** 和 **模拟点击/滑动** 等操作。

## 原理

```
Mac (Python) ──USB──▶ iOS 设备 (WebDriverAgent)
                       │
                tidevice 管理连接
                facebook-wda 发送指令
```

核心依赖：
- **[tidevice](https://github.com/alibaba/taobao-iphone-device)** — 阿里巴巴开源工具，用于与 iOS 设备通信、启动 WDA
- **[facebook-wda](https://github.com/openatx/facebook-wda)** — WebDriverAgent 的 Python 客户端，实现截图/点击/滑动
- **[WebDriverAgent](https://github.com/appium/WebDriverAgent)** — Facebook 开源的 iOS 自动化测试框架，运行在设备上

## 前置条件

### 1. 安装 Python 环境依赖

```bash
pip install -r requirements.txt
```

### 2. 安装 WebDriverAgent 到 iOS 设备

WDA 需要通过 Xcode 编译并安装到设备上，这是**最关键的一步**：

1. **安装 Xcode**（从 App Store）

2. **克隆 WebDriverAgent**：
   ```bash
   git clone https://github.com/appium/WebDriverAgent.git
   cd WebDriverAgent
   ```

3. **用 Xcode 打开项目**：
   ```bash
   open WebDriverAgent.xcodeproj
   ```

4. **配置签名**：
   - 在 Xcode 中选择 `WebDriverAgentRunner` target
   - 在 "Signing & Capabilities" 中设置你的 Apple Developer Team
   - 修改 Bundle Identifier 为唯一值，比如 `com.yourname.WebDriverAgentRunner`

5. **编译安装到设备**：
   - 连接 iOS 设备，在 Xcode 上方选择你的设备
   - 选择 `WebDriverAgentRunner` scheme
   - 点击 `Product` → `Test` 或按 `Cmd+U`

6. **信任开发者**（首次安装时）：
   - 在 iOS 设备上进入 `设置` → `通用` → `VPN与设备管理`
   - 找到你的开发者证书，点击「信任」

> **提示**：如果没有付费开发者账号，可以使用免费的 Apple ID，但需要每 7 天重新签名一次。

### 3. 确认 WDA Bundle ID

安装完成后，确保脚本中的 `WDA_BUNDLE_ID` 与你安装的一致：

```python
WDA_BUNDLE_ID = "com.facebook.WebDriverAgentRunner.xctrunner"
```

如果你修改了 Bundle ID，请同步修改脚本中的值。

## 使用方法

### 命令行模式

```bash
# 列出已连接的设备
python ios_controller.py list

# 截图（自动保存到 ./screenshots/）
python ios_controller.py screenshot

# 截图并指定保存路径
python ios_controller.py screenshot -o my_shot.png

# 点击坐标 (200, 300)
python ios_controller.py tap 200 300

# 滑动：从 (100,500) 滑到 (100,200)
python ios_controller.py swipe 100 500 100 200

# 启动 Safari
python ios_controller.py launch com.apple.mobilesafari

# 进入交互模式
python ios_controller.py interactive
```

### 交互模式

```bash
python ios_controller.py interactive
```

进入后可以连续输入命令：
```
ClawPad> ss                         # 截图
ClawPad> tap 200 300                # 点击
ClawPad> doubletap 200 300          # 双击
ClawPad> longpress 200 300          # 长按
ClawPad> longpress 200 300 2.0      # 长按 2 秒
ClawPad> swipe 100 500 100 200      # 滑动
ClawPad> swipeup                    # 向上翻页
ClawPad> swipedown                  # 向下翻页
ClawPad> type Hello World           # 输入文本
ClawPad> home                       # 按 Home 键
ClawPad> info                       # 查看设备信息
ClawPad> size                       # 查看屏幕尺寸
ClawPad> launch com.apple.Preferences  # 打开设置
ClawPad> quit                       # 退出
```

### 作为 Python 库使用

```python
from ios_controller import iOSController

# 方式 1：with 语句自动管理连接
with iOSController() as ctrl:
    # 截图
    ctrl.screenshot("test.png")

    # 点击坐标
    ctrl.tap(200, 300)

    # 滑动
    ctrl.swipe(100, 500, 100, 200)

    # 输入文本
    ctrl.type_text("Hello")

    # 获取屏幕尺寸
    w, h = ctrl.get_screen_size()
    print(f"屏幕: {w}x{h}")

# 方式 2：手动管理
ctrl = iOSController(udid="your-device-udid")
ctrl.connect()
ctrl.screenshot()
ctrl.tap(100, 200)
ctrl.disconnect()
```

## 多设备支持

如果连接了多台设备，使用 `-u` 参数指定 UDID：

```bash
# 先列出设备查看 UDID
python ios_controller.py list

# 指定设备操作
python ios_controller.py -u <UDID> screenshot
python ios_controller.py -u <UDID> interactive
```

## 常见问题

### Q: 提示 "WebDriverAgent 未能启动"
- 确认 WDA 已成功安装到设备（Xcode 编译通过）
- 确认设备已信任开发者证书
- 检查 `WDA_BUNDLE_ID` 是否正确

### Q: 提示找不到设备
- 确认 USB 线已连接
- 在设备上点击「信任此电脑」
- 尝试 `python -m tidevice list` 确认设备可见

### Q: 点击坐标不准确
- WDA 使用的是**逻辑坐标**（point），不是物理像素
- 使用 `size` 命令查看逻辑分辨率
- 可以先截图，用图片查看器确认坐标位置

### Q: 免费开发者账号签名过期
- 免费 Apple ID 签名有效期 7 天
- 过期后需要重新用 Xcode 编译安装 WDA

## 常用 Bundle ID

| 应用 | Bundle ID |
|------|-----------|
| Safari | `com.apple.mobilesafari` |
| 设置 | `com.apple.Preferences` |
| 相机 | `com.apple.camera` |
| 照片 | `com.apple.mobileslideshow` |
| App Store | `com.apple.AppStore` |
| 备忘录 | `com.apple.mobilenotes` |

## License

MIT
