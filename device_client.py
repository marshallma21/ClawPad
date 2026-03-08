#!/usr/bin/env python3
"""
ClawPad Device Client (C1) - 设备端客户端
==========================================

在本地连接 iOS 设备，通过 WebSocket 注册到 ClawPad Server，
并持续监听服务器转发的控制指令。

架构角色: C1 (本脚本连接物理设备并执行指令)

使用:
    python device_client.py --server http://<server>:8200 [-u UDID] [-p WDA_PORT]

流程:
    1. 通过 USB 连接本地 iOS 设备
    2. 通过 WebSocket 连接 ClawPad Server
    3. 注册设备信息，获取 device_id 和 token
    4. 打印鉴权信息（供 C2 使用）
    5. 持续监听并执行 Server 转发的控制指令
    6. 断线自动重连
"""

import argparse
import asyncio
import base64
import json
import logging
import signal
import sys
import time

from ios_controller import iOSController, WDA_PORT

logger = logging.getLogger("clawpad.device_client")

DEFAULT_SERVER_URL = "http://localhost:8200"
RECONNECT_DELAY = 5  # 重连间隔（秒）


class DeviceClient:
    """
    C1 设备客户端：连接本地 iOS 设备，注册到 Server，执行远程指令。
    """

    def __init__(
        self,
        server_url: str = DEFAULT_SERVER_URL,
        udid: str | None = None,
        wda_port: int = WDA_PORT,
    ):
        # WebSocket URL
        ws_base = server_url.replace("https://", "wss://").replace(
            "http://", "ws://"
        )
        self.ws_url = f"{ws_base.rstrip('/')}/ws/device"
        self.server_url = server_url

        # iOS 控制器
        self.controller = iOSController(udid=udid, wda_port=wda_port)

        # 状态
        self.device_id: str | None = None
        self.token: str | None = None
        self._running = True
        self._ws = None            # 当前 WebSocket 连接
        self._loop = None          # 事件循环引用
        self._sleep_task = None    # 当前 sleep 任务（用于取消重连等待）

    # ── 主循环（带自动重连）──────────────────────────────────────────────

    async def run_forever(self) -> None:
        """主运行循环，断线后自动重连。"""
        self._loop = asyncio.get_running_loop()

        # 连接本地 iOS 设备（同步操作，在线程池中运行）
        try:
            await self._loop.run_in_executor(None, self.controller.connect)
        except Exception as e:
            print(f"\n❌ 无法连接 iOS 设备: {e}")
            return

        while self._running:
            try:
                await self._connect_and_serve()
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                logger.warning(f"连接中断: {e}")
                print(
                    f"\n⚠️  与服务器的连接中断: {e}"
                    f"\n   {RECONNECT_DELAY}s 后重连..."
                )
                try:
                    self._sleep_task = asyncio.ensure_future(
                        asyncio.sleep(RECONNECT_DELAY)
                    )
                    await self._sleep_task
                except asyncio.CancelledError:
                    break
                finally:
                    self._sleep_task = None

        # 清理
        try:
            self.controller.disconnect()
        except Exception:
            pass

    async def _connect_and_serve(self) -> None:
        """连接 Server 并持续服务。"""
        try:
            import websockets
        except ImportError:
            print("❌ 缺少 websockets 库，请运行: pip install websockets")
            self._running = False
            return

        # 收集设备信息
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(
            None, self.controller.get_device_info
        )
        w, h = await loop.run_in_executor(
            None, self.controller.get_screen_size
        )

        print(f"\n🔗 正在连接服务器: {self.ws_url}")

        async with websockets.connect(
            self.ws_url,
            ping_interval=30,
            ping_timeout=10,
            max_size=50 * 1024 * 1024,  # 50MB（截图可能较大）
        ) as ws:
            self._ws = ws

            # 1. 发送注册消息
            reg_msg = {
                "type": "register",
                "udid": self.controller.udid,
                "device_name": info.get("设备名称", "Unknown"),
                "model": info.get("型号", "Unknown"),
                "ios_version": info.get("iOS版本", "Unknown"),
                "screen_width": w,
                "screen_height": h,
            }
            await ws.send(json.dumps(reg_msg))

            # 2. 等待注册响应
            resp = json.loads(await ws.recv())
            if resp.get("type") != "registered":
                raise RuntimeError(
                    f"注册失败: {resp.get('message', '未知错误')}"
                )

            self.device_id = resp["device_id"]
            self.token = resp["token"]

            self._print_credentials()

            # 3. 持续监听指令
            print("⏳ 等待指令中... (Ctrl+C 退出)\n")

            try:
                async for msg_str in ws:
                    if not self._running:
                        break
                    msg = json.loads(msg_str)

                    if msg.get("type") == "command":
                        # 在线程池中执行指令（避免阻塞事件循环）
                        result = await loop.run_in_executor(
                            None, self._execute_command, msg
                        )
                        if self._running:
                            await ws.send(json.dumps(result))

                    elif msg.get("type") == "ping":
                        await ws.send(json.dumps({"type": "pong"}))
            except asyncio.CancelledError:
                pass
            finally:
                self._ws = None

    # ── 指令执行 ──────────────────────────────────────────────────────────

    def _execute_command(self, msg: dict) -> dict:
        """在本地执行控制指令并返回结果。运行在线程池中。"""
        action = msg["action"]
        params = msg.get("params", {})
        request_id = msg["request_id"]

        timestamp = time.strftime("%H:%M:%S")
        print(f"  [{timestamp}] 📥 执行指令: {action} {params or ''}")

        try:
            result_data = {}

            if action == "screenshot":
                path = self.controller.screenshot()
                with open(path, "rb") as f:
                    img_b64 = base64.b64encode(f.read()).decode()
                result_data = {"image_base64": img_b64, "path": path}

            elif action == "tap":
                self.controller.tap(params["x"], params["y"])

            elif action == "double_tap":
                self.controller.double_tap(params["x"], params["y"])

            elif action == "long_press":
                self.controller.long_press(
                    params["x"], params["y"],
                    params.get("duration", 1.0),
                )

            elif action == "swipe":
                self.controller.swipe(
                    params["x1"], params["y1"],
                    params["x2"], params["y2"],
                    params.get("duration", 0.5),
                )

            elif action == "swipe_up":
                self.controller.swipe_up()

            elif action == "swipe_down":
                self.controller.swipe_down()

            elif action == "type_text":
                self.controller.type_text(params["text"])

            elif action == "press_home":
                self.controller.press_home()

            elif action == "press_volume_up":
                self.controller.press_volume_up()

            elif action == "press_volume_down":
                self.controller.press_volume_down()

            elif action == "launch_app":
                self.controller.launch_app(params["bundle_id"])

            elif action == "get_device_info":
                result_data = self.controller.get_device_info()

            elif action == "get_screen_size":
                w, h = self.controller.get_screen_size()
                result_data = {"width": w, "height": h}

            else:
                return {
                    "type": "result",
                    "request_id": request_id,
                    "status": "error",
                    "error": f"未知指令: {action}",
                }

            print(f"  [{timestamp}] ✅ 完成: {action}")
            return {
                "type": "result",
                "request_id": request_id,
                "status": "ok",
                "data": result_data,
            }

        except Exception as e:
            print(f"  [{timestamp}] ❌ 失败: {action} -> {e}")
            return {
                "type": "result",
                "request_id": request_id,
                "status": "error",
                "error": str(e),
            }

    # ── 输出凭证信息 ──────────────────────────────────────────────────────

    def _print_credentials(self) -> None:
        """打印注册成功后的鉴权信息。"""
        print(f"""
╔══════════════════════════════════════════════════════════════╗
║  设备注册成功！                                              ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  设备 ID :  {self.device_id:<49}║
║  Token   :  {self.token:<49}║
║                                                              ║
║  将以上信息提供给 C2 控制端使用:                             ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
python ios_controller.py -r \\
  --server {self.server_url} \\
  --device {self.device_id} \\
  --token {self.token} \\
  screenshot                                                
╔══════════════════════════════════════════════════════════════╗
║  或进入交互模式:                                             ║
╚══════════════════════════════════════════════════════════════╝
python ios_controller.py -r \\
  --server {self.server_url} \\
  --device {self.device_id} \\
  --token {self.token} \\
  interactive
""")

    # ── 停止 ──────────────────────────────────────────────────────────────

    def stop(self) -> None:
        """停止客户端（可从信号处理器中安全调用）。"""
        self._running = False

        if self._loop is None or self._loop.is_closed():
            return

        # 关闭 WebSocket 连接，让 async for 循环退出
        if self._ws is not None:
            asyncio.run_coroutine_threadsafe(
                self._ws.close(), self._loop
            )

        # 取消重连等待中的 sleep
        if self._sleep_task is not None and not self._sleep_task.done():
            self._loop.call_soon_threadsafe(self._sleep_task.cancel)


# ─── 入口 ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="ClawPad Device Client (C1) - 连接设备并注册到服务器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s --server http://localhost:8200
  %(prog)s --server http://192.168.1.100:8200 -u <UDID>
  %(prog)s --server http://myserver:8200 -p 8100
        """,
    )
    parser.add_argument(
        "--server", default=DEFAULT_SERVER_URL,
        help=f"ClawPad Server 地址 (默认: {DEFAULT_SERVER_URL})",
    )
    parser.add_argument(
        "-u", "--udid",
        help="指定 iOS 设备 UDID（多设备时使用）",
    )
    parser.add_argument(
        "-p", "--port", type=int, default=WDA_PORT,
        help=f"WDA 端口号 (默认: {WDA_PORT})",
    )

    args = parser.parse_args()

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║              🐾 ClawPad Device Client (C1)                   ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  Server :  {args.server:<50}║
║  UDID   :  {(args.udid or '自动检测'):<46}║
║  WDA端口:  {str(args.port):<50}║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
""")

    client = DeviceClient(
        server_url=args.server,
        udid=args.udid,
        wda_port=args.port,
    )

    # 优雅退出
    def _signal_handler(sig, frame):
        print("\n\n🛑 正在停止...")
        client.stop()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )

    try:
        asyncio.run(client.run_forever())
    except KeyboardInterrupt:
        pass

    print("👋 设备客户端已停止")


if __name__ == "__main__":
    main()
