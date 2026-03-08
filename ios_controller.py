#!/usr/bin/env python3
"""
ClawPad - iOS Device Controller for macOS
==========================================
通过 USB 连接 iOS 设备（iPhone/iPad），实现截图和模拟点击功能。

依赖：
  - pymobiledevice3：与 iOS 设备通信
  - wda (facebook-wda)：通过 WebDriverAgent 实现截图和点击
  - tidevice：管理 iOS 设备和启动 WDA

使用前准备：
  1. pip install -r requirements.txt
  2. iOS 设备通过 USB 连接到 Mac
  3. 设备已信任此电脑
  4. 安装 WebDriverAgent 到 iOS 设备（见 README）
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    import wda
except ImportError:
    print("错误: 缺少 facebook-wda 库，请运行: pip install facebook-wda")
    sys.exit(1)

try:
    import tidevice
    from tidevice import Device as TiDevice
except ImportError:
    print("错误: 缺少 tidevice 库，请运行: pip install tidevice[openssl]")
    sys.exit(1)


# ─── 配置 ───────────────────────────────────────────────────────────────────

WDA_BUNDLE_ID = "com.facebook.WebDriverAgentRunner.xctrunner"
WDA_PORT = 8100
SCREENSHOT_DIR = Path("./screenshots")


# ─── 设备管理 ─────────────────────────────────────────────────────────────────

class iOSController:
    """iOS 设备控制器：截图、点击、滑动等操作。"""

    def __init__(self, udid: str | None = None, wda_port: int = WDA_PORT):
        self.wda_port = wda_port
        self.udid = udid
        self._wda_process = None
        self._client: wda.Client | None = None
        self._device: TiDevice | None = None

    # ── 连接 ──────────────────────────────────────────────────────────────

    def connect(self) -> None:
        """连接到 iOS 设备并启动 WDA。"""
        print("🔍 正在查找 iOS 设备...")
        self._device = TiDevice(self.udid)
        info = self._device.device_info()
        name = info.get("DeviceName", "未知设备")
        ios_ver = info.get("ProductVersion", "?")
        model = info.get("ProductType", "?")
        self.udid = self._device.udid
        print(f"✅ 已找到设备: {name} ({model}, iOS {ios_ver})")
        print(f"   UDID: {self.udid}")

        # 启动 WebDriverAgent
        self._start_wda()

        # 连接 WDA 客户端
        print(f"🔗 正在连接 WebDriverAgent (端口 {self.wda_port})...")
        self._client = wda.Client(f"http://localhost:{self.wda_port}")

        # 等待 WDA 启动就绪
        self._wait_for_wda()

        win_size = self._client.window_size()
        print(f"✅ 已连接! 屏幕尺寸: {win_size.width} x {win_size.height}")

    def _start_wda(self) -> None:
        """通过 tidevice 启动 WebDriverAgent。"""
        print(f"🚀 正在启动 WebDriverAgent...")
        # 使用 tidevice 做端口转发并启动 WDA
        cmd = [
            sys.executable, "-m", "tidevice",
            "-u", self.udid,
            "wdaproxy",
            "-B", WDA_BUNDLE_ID,
            "--port", str(self.wda_port),
        ]
        self._wda_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # 给 WDA 一些启动时间
        time.sleep(3)

    def _wait_for_wda(self, timeout: int = 30) -> None:
        """等待 WDA 准备就绪。"""
        start = time.time()
        while time.time() - start < timeout:
            try:
                status = self._client.status()
                if status:
                    return
            except Exception:
                time.sleep(1)
        raise TimeoutError(f"WebDriverAgent 在 {timeout} 秒内未能启动")

    def disconnect(self) -> None:
        """断开连接并清理资源。"""
        if self._wda_process:
            self._wda_process.terminate()
            self._wda_process.wait(timeout=5)
            self._wda_process = None
        self._client = None
        print("👋 已断开连接")

    # ── 截图 ──────────────────────────────────────────────────────────────

    def screenshot(self, save_path: str | None = None) -> str:
        """
        获取设备截图并保存到本地。

        Args:
            save_path: 保存路径，为 None 则自动生成文件名

        Returns:
            截图文件的路径
        """
        self._ensure_connected()

        if save_path is None:
            SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            save_path = str(SCREENSHOT_DIR / f"screenshot_{timestamp}.png")

        self._client.screenshot(save_path)
        print(f"📸 截图已保存: {save_path}")
        return save_path

    # ── 点击/触摸 ─────────────────────────────────────────────────────────

    def tap(self, x: int, y: int) -> None:
        """
        模拟在指定坐标点击。

        Args:
            x: 横坐标
            y: 纵坐标
        """
        self._ensure_connected()
        self._client.click(x, y)
        print(f"👆 点击: ({x}, {y})")

    def double_tap(self, x: int, y: int) -> None:
        """模拟双击。"""
        self._ensure_connected()
        self._client.double_click(x, y)
        print(f"👆👆 双击: ({x}, {y})")

    def long_press(self, x: int, y: int, duration: float = 1.0) -> None:
        """
        模拟长按。

        Args:
            x: 横坐标
            y: 纵坐标
            duration: 长按持续时间（秒）
        """
        self._ensure_connected()
        # WDA 长按通过 touchAndHold 实现
        self._client.session().tap_hold(x, y, duration)
        print(f"👇 长按: ({x}, {y}) 持续 {duration}s")

    # ── 滑动 ──────────────────────────────────────────────────────────────

    def swipe(self, x1: int, y1: int, x2: int, y2: int,
              duration: float = 0.1) -> None:
        """
        模拟从 (x1,y1) 滑动到 (x2,y2)。

        Args:
            x1, y1: 起点坐标
            x2, y2: 终点坐标
            duration: 滑动持续时间（秒）
        """
        self._ensure_connected()
        self._client.swipe(x1, y1, x2, y2, duration)
        print(f"👉 滑动: ({x1},{y1}) -> ({x2},{y2})")

    def swipe_up(self) -> None:
        """向上滑动（翻页）。"""
        size = self._client.window_size()
        cx = size.width // 2
        self.swipe(cx, size.height * 3 // 4, cx, size.height // 4)

    def swipe_down(self) -> None:
        """向下滑动。"""
        size = self._client.window_size()
        cx = size.width // 2
        self.swipe(cx, size.height // 4, cx, size.height * 3 // 4)

    # ── 文本输入 ──────────────────────────────────────────────────────────

    def type_text(self, text: str) -> None:
        """
        在当前焦点输入框中输入文本。

        Args:
            text: 要输入的文本
        """
        self._ensure_connected()
        self._client.send_keys(text)
        print(f"⌨️  输入: {text}")

    # ── Home 键 / 其他按钮 ───────────────────────────────────────────────

    def press_home(self) -> None:
        """按下 Home 键。"""
        self._ensure_connected()
        self._client.press("home")
        print("🏠 按下 Home 键")

    def press_volume_up(self) -> None:
        """按下音量+。"""
        self._ensure_connected()
        self._client.press("volumeUp")
        print("🔊 音量+")

    def press_volume_down(self) -> None:
        """按下音量-。"""
        self._ensure_connected()
        self._client.press("volumeDown")
        print("🔉 音量-")

    # ── 应用管理 ──────────────────────────────────────────────────────────

    def launch_app(self, bundle_id: str) -> None:
        """
        启动指定应用。

        Args:
            bundle_id: 应用 Bundle ID，如 'com.apple.mobilesafari'
        """
        self._ensure_connected()
        self._client.session(bundle_id)
        print(f"🚀 已启动应用: {bundle_id}")

    # ── 设备信息 ──────────────────────────────────────────────────────────

    def get_device_info(self) -> dict:
        """获取设备详细信息。"""
        self._ensure_connected()
        info = self._device.device_info()
        return {
            "设备名称": info.get("DeviceName"),
            "型号": info.get("ProductType"),
            "iOS版本": info.get("ProductVersion"),
            "UDID": self.udid,
            "屏幕尺寸": f"{self._client.window_size().width}x{self._client.window_size().height}",
        }

    def get_screen_size(self) -> tuple[int, int]:
        """获取屏幕尺寸 (width, height)。"""
        self._ensure_connected()
        size = self._client.window_size()
        return (size.width, size.height)

    # ── 内部方法 ──────────────────────────────────────────────────────────

    def _ensure_connected(self) -> None:
        if self._client is None:
            raise RuntimeError("未连接设备，请先调用 connect()")

    # ── 上下文管理 ────────────────────────────────────────────────────────

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()


# ─── 远程客户端 (C2) ──────────────────────────────────────────────────────────

SERVICE_DEFAULT_PORT = 8200
SERVICE_DEFAULT_URL = f"http://localhost:{SERVICE_DEFAULT_PORT}"


class ClawPadClient:
    """
    C2 控制客户端：通过 HTTP API + Token 鉴权与 ClawPad Server 通信。

    接口与 iOSController 兼容，可直接在 interactive_mode 中使用。

    架构: C2 (本客户端) ──HTTP──> S (Server) ──WS──> C1 (设备端)
    """

    def __init__(
        self,
        server_url: str,
        device_id: str,
        token: str,
    ):
        self.server_url = server_url.rstrip("/")
        self.device_id = device_id
        self.token = token
        self._base_path = f"/devices/{device_id}"
        self._auth_header = f"Bearer {token}"
        self._check_connection()

    def _check_connection(self) -> None:
        """检查服务可达且设备已注册。"""
        try:
            # /devices 不需要鉴权
            data = self._get_noauth("/devices")
            devices = data.get("devices", [])
            found = any(d["device_id"] == self.device_id for d in devices)
            if not found:
                raise ConnectionError(
                    f"❌ 设备 {self.device_id} 未在服务器上注册\n"
                    f"   已注册设备: {[d['device_id'] for d in devices]}"
                )
            print(f"""
╔══════════════════════════════════════════════════════════════╗
║                   ClawPad User Client (C2)                   ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  ClawPad Server: {self.server_url:<44}║
║  Target Device:  {self.device_id:<44}║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
""")
        except urllib.error.URLError as e:
            raise ConnectionError(
                f"❌ 无法连接到 ClawPad Server ({self.server_url})\n"
                f"   请先启动服务器: python server.py\n"
                f"   错误: {e}"
            )

    # ── HTTP 封装 ─────────────────────────────────────────────────────────

    def _get_noauth(self, path: str) -> dict:
        """无鉴权 GET 请求。"""
        url = f"{self.server_url}{path}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())

    def _get(self, path: str) -> dict:
        """带鉴权的 GET 请求（设备级别路径）。"""
        url = f"{self.server_url}{self._base_path}{path}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", self._auth_header)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            self._handle_http_error(e)
        except urllib.error.URLError as e:
            raise ConnectionError(f"无法连接到服务器: {e}")

    def _get_binary(self, path: str) -> bytes:
        """带鉴权的 GET 请求，返回原始字节。"""
        url = f"{self.server_url}{self._base_path}{path}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", self._auth_header)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            self._handle_http_error(e)
        except urllib.error.URLError as e:
            raise ConnectionError(f"无法连接到服务器: {e}")

    def _post(self, path: str, data: dict | None = None) -> dict:
        """带鉴权的 POST 请求（设备级别路径）。"""
        url = f"{self.server_url}{self._base_path}{path}"
        body = json.dumps(data).encode() if data else b"{}"
        req = urllib.request.Request(
            url, data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": self._auth_header,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            self._handle_http_error(e)
        except urllib.error.URLError as e:
            raise ConnectionError(f"无法连接到服务器: {e}")

    @staticmethod
    def _handle_http_error(e: urllib.error.HTTPError) -> None:
        """统一处理 HTTP 错误响应。"""
        try:
            detail = json.loads(e.read().decode()).get("detail", str(e))
        except Exception:
            detail = str(e)
        if e.code == 401:
            raise PermissionError(f"鉴权失败: {detail}")
        elif e.code == 403:
            raise PermissionError(f"权限不足: {detail}")
        elif e.code == 404:
            raise RuntimeError(f"设备不存在或已离线: {detail}")
        elif e.code == 503:
            raise RuntimeError(f"设备离线: {detail}")
        elif e.code == 504:
            raise TimeoutError(f"设备响应超时: {detail}")
        else:
            raise RuntimeError(f"服务器错误 ({e.code}): {detail}")

    # ── 与 iOSController 兼容的接口 ─────────────────────────────────────

    def screenshot(self, save_path: str | None = None) -> str:
        if save_path is None:
            SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            save_path = str(SCREENSHOT_DIR / f"screenshot_{timestamp}.png")
        img = self._get_binary("/screenshot")
        with open(save_path, "wb") as f:
            f.write(img)
        print(f"📸 截图已保存: {save_path}")
        return save_path

    def tap(self, x: int, y: int) -> None:
        self._post("/tap", {"x": x, "y": y})
        print(f"👆 点击: ({x}, {y})")

    def double_tap(self, x: int, y: int) -> None:
        self._post("/doubletap", {"x": x, "y": y})
        print(f"👆👆 双击: ({x}, {y})")

    def long_press(self, x: int, y: int, duration: float = 1.0) -> None:
        self._post("/longpress", {"x": x, "y": y, "duration": duration})
        print(f"👇 长按: ({x}, {y}) 持续 {duration}s")

    def swipe(self, x1: int, y1: int, x2: int, y2: int,
              duration: float = 0.5) -> None:
        self._post("/swipe", {"x1": x1, "y1": y1,
                               "x2": x2, "y2": y2, "duration": duration})
        print(f"👉 滑动: ({x1},{y1}) -> ({x2},{y2})")

    def swipe_up(self) -> None:
        self._post("/swipe_up")
        print("👆 上滑")

    def swipe_down(self) -> None:
        self._post("/swipe_down")
        print("👇 下滑")

    def type_text(self, text: str) -> None:
        self._post("/type", {"text": text})
        print(f"⌨️  输入: {text}")

    def press_home(self) -> None:
        self._post("/home")
        print("🏠 按下 Home 键")

    def press_volume_up(self) -> None:
        self._post("/volume_up")
        print("🔊 音量+")

    def press_volume_down(self) -> None:
        self._post("/volume_down")
        print("🔉 音量-")

    def launch_app(self, bundle_id: str) -> None:
        self._post("/launch", {"bundle_id": bundle_id})
        print(f"🚀 已启动应用: {bundle_id}")

    def get_device_info(self) -> dict:
        return self._get("/info")

    def get_screen_size(self) -> tuple[int, int]:
        data = self._get("/size")
        return (data["width"], data["height"])


# ─── 命令行接口 ────────────────────────────────────────────────────────────────

def list_devices():
    """列出所有已连接的 iOS 设备。"""
    print("🔍 正在扫描已连接的 iOS 设备...\n")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "tidevice", "list", "--json"],
            capture_output=True, text=True
        )
        devices = json.loads(result.stdout) if result.stdout.strip() else []
        if not devices:
            print("❌ 未检测到任何 iOS 设备。")
            print("   请确保：")
            print("   1. 设备已通过 USB 连接")
            print("   2. 设备已解锁并信任此电脑")
            return

        print(f"找到 {len(devices)} 台设备:\n")
        for i, dev in enumerate(devices, 1):
            udid = dev.get("udid", "N/A")
            name = dev.get("name", "未知")
            conn = dev.get("connection_type", "USB")
            print(f"  [{i}] {name}")
            print(f"      UDID: {udid}")
            print(f"      连接方式: {conn}")
            print()
    except Exception as e:
        print(f"❌ 扫描设备时出错: {e}")


def interactive_mode(controller):
    """进入交互模式，可以实时操作设备。支持 iOSController 和 ClawPadClient。"""

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║                  ClawPad Interactive Mode                    ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  screenshot / ss      - 截图                                 ║
║  tap <x> <y>          - 点击坐标                             ║
║  doubletap <x> <y>    - 双击坐标                             ║
║  longpress <x> <y>    - 长按坐标                             ║
║  swipe <x1> <y1> <x2> <y2> - 滑动                            ║
║  swipeup / swipedown  - 上/下滑动                            ║
║  type <text>          - 输入文本                             ║
║  home                 - 按 Home 键                           ║
║  info                 - 设备信息                             ║
║  size                 - 屏幕尺寸                             ║
║  launch <bundle_id>   - 启动应用                             ║
║  quit / exit          - 退出                                 ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
""")

    while True:
        try:
            cmd = input("ClawPad> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not cmd:
            continue

        parts = cmd.split()
        action = parts[0].lower()

        try:
            if action in ("quit", "exit", "q"):
                break

            elif action in ("screenshot", "ss"):
                path = parts[1] if len(parts) > 1 else None
                controller.screenshot(path)

            elif action == "tap" and len(parts) == 3:
                controller.tap(int(parts[1]), int(parts[2]))

            elif action == "doubletap" and len(parts) == 3:
                controller.double_tap(int(parts[1]), int(parts[2]))

            elif action == "longpress" and len(parts) >= 3:
                dur = float(parts[3]) if len(parts) > 3 else 1.0
                controller.long_press(int(parts[1]), int(parts[2]), dur)

            elif action == "swipe" and len(parts) >= 5:
                dur = float(parts[5]) if len(parts) > 5 else 0.5
                controller.swipe(
                    int(parts[1]), int(parts[2]),
                    int(parts[3]), int(parts[4]), dur
                )

            elif action == "swipeup":
                controller.swipe_up()

            elif action == "swipedown":
                controller.swipe_down()

            elif action == "type" and len(parts) > 1:
                controller.type_text(" ".join(parts[1:]))

            elif action == "home":
                controller.press_home()

            elif action == "info":
                info = controller.get_device_info()
                for k, v in info.items():
                    print(f"  {k}: {v}")

            elif action == "size":
                w, h = controller.get_screen_size()
                print(f"  屏幕尺寸: {w} x {h}")

            elif action == "launch" and len(parts) > 1:
                controller.launch_app(parts[1])

            else:
                print(f"  ❓ 未知命令: {cmd}")
                print("  输入 help 查看可用命令")

        except Exception as e:
            print(f"  ❌ 错误: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="ClawPad - iOS 设备控制器（截图、点击、滑动）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
直连模式（本地 USB 设备）:
  %(prog)s list                          # 列出本地设备
  %(prog)s screenshot                    # 截图
  %(prog)s tap 200 300                   # 点击坐标
  %(prog)s interactive                   # 交互模式

服务架构（C1-S-C2）:
  %(prog)s serve                         # 启动中心服务器 (S)
  %(prog)s register --server URL         # 注册本地设备到服务器 (C1)
  %(prog)s -r --server URL \\            # 远程控制设备 (C2)
           --device ID --token TOKEN screenshot
  %(prog)s -r --server URL \\            # 远程交互模式 (C2)
           --device ID --token TOKEN interactive
        """,
    )
    parser.add_argument("-u", "--udid", help="指定设备 UDID（多设备时使用）")
    parser.add_argument("-p", "--port", type=int, default=WDA_PORT,
                        help=f"WDA 端口号（默认: {WDA_PORT}）")

    # C2 远程模式参数
    remote_group = parser.add_argument_group("远程模式 (C2)")
    remote_group.add_argument("-r", "--remote", action="store_true",
                              help="通过 Server 远程控制设备")
    remote_group.add_argument("--server", default=SERVICE_DEFAULT_URL,
                              help=f"Server 地址（默认: {SERVICE_DEFAULT_URL}）")
    remote_group.add_argument("--device", help="目标设备 ID")
    remote_group.add_argument("--token", help="鉴权 Token")

    sub = parser.add_subparsers(dest="command", help="命令")

    # list
    sub.add_parser("list", help="列出已连接的 iOS 设备")

    # screenshot
    p_ss = sub.add_parser("screenshot", aliases=["ss"], help="截图")
    p_ss.add_argument("-o", "--output", help="截图保存路径")

    # tap
    p_tap = sub.add_parser("tap", help="模拟点击")
    p_tap.add_argument("x", type=int, help="横坐标")
    p_tap.add_argument("y", type=int, help="纵坐标")

    # swipe
    p_swipe = sub.add_parser("swipe", help="模拟滑动")
    p_swipe.add_argument("x1", type=int)
    p_swipe.add_argument("y1", type=int)
    p_swipe.add_argument("x2", type=int)
    p_swipe.add_argument("y2", type=int)
    p_swipe.add_argument("-d", "--duration", type=float, default=0.5,
                         help="滑动持续时间（秒）")

    # launch
    p_launch = sub.add_parser("launch", help="启动应用")
    p_launch.add_argument("bundle_id", help="应用 Bundle ID")

    # interactive
    sub.add_parser("interactive", aliases=["i"], help="交互模式")

    # serve (启动服务器 S)
    p_serve = sub.add_parser("serve", help="启动中心转发服务器 (S)")
    p_serve.add_argument("--host", default="0.0.0.0",
                         help="监听地址 (默认: 0.0.0.0)")
    p_serve.add_argument("--service-port", type=int, default=SERVICE_DEFAULT_PORT,
                         help=f"服务端口 (默认: {SERVICE_DEFAULT_PORT})")

    # register (注册本地设备到服务器 C1)
    p_reg = sub.add_parser("register", aliases=["reg"],
                           help="注册本地设备到服务器 (C1)")
    p_reg.add_argument("--server", default=SERVICE_DEFAULT_URL,
                       help=f"Server 地址（默认: {SERVICE_DEFAULT_URL}）")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "list":
        list_devices()
        return

    if args.command == "serve":
        from server import start_server
        start_server(
            host=args.host,
            port=args.service_port,
        )
        return

    if args.command in ("register", "reg"):
        from device_client import DeviceClient
        import asyncio
        client = DeviceClient(
            server_url=args.server,
            udid=args.udid,
            wda_port=args.port,
        )
        try:
            asyncio.run(client.run_forever())
        except KeyboardInterrupt:
            pass
        return

    # ── 远程模式 (C2)：通过 Server 远程控制设备 ──────────────────────────
    if args.remote:
        if not args.device or not args.token:
            print("❌ 远程模式需要 --device 和 --token 参数")
            print("   用法: python ios_controller.py -r \\") 
            print("           --server URL --device ID --token TOKEN 命令")
            print("\n   提示: 先运行 'python ios_controller.py register' 获取凭证")
            sys.exit(1)

        try:
            client = ClawPadClient(
                server_url=args.server,
                device_id=args.device,
                token=args.token,
            )
        except (ConnectionError, PermissionError) as e:
            print(str(e))
            sys.exit(1)

        try:
            if args.command in ("screenshot", "ss"):
                client.screenshot(args.output)
            elif args.command == "tap":
                client.tap(args.x, args.y)
            elif args.command == "swipe":
                client.swipe(args.x1, args.y1, args.x2, args.y2, args.duration)
            elif args.command == "launch":
                client.launch_app(args.bundle_id)
            elif args.command in ("interactive", "i"):
                interactive_mode(client)
        except KeyboardInterrupt:
            print("\n中断退出")
        except Exception as e:
            print(f"\n❌ 错误: {e}")
            sys.exit(1)
        return

    # ── 直连模式：本地 USB 直接连接设备 ──────────────────────────────────
    ctrl = iOSController(udid=args.udid, wda_port=args.port)

    try:
        ctrl.connect()

        if args.command in ("screenshot", "ss"):
            ctrl.screenshot(args.output)

        elif args.command == "tap":
            ctrl.tap(args.x, args.y)

        elif args.command == "swipe":
            ctrl.swipe(args.x1, args.y1, args.x2, args.y2, args.duration)

        elif args.command == "launch":
            ctrl.launch_app(args.bundle_id)

        elif args.command in ("interactive", "i"):
            interactive_mode(ctrl)

    except KeyboardInterrupt:
        print("\n中断退出")
    except Exception as e:
        print(f"\n❌ 错误: {e}")
        sys.exit(1)
    finally:
        ctrl.disconnect()


if __name__ == "__main__":
    main()
