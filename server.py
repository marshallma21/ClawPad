#!/usr/bin/env python3
"""
ClawPad Server (S) - 中心转发服务器
====================================

架构:
    C1 (设备客户端) ──WebSocket──> S (本服务器) <──HTTP── C2 (控制客户端)

流程:
    1. C1 在本地连接 iOS 设备，通过 WebSocket 注册到 S
    2. S 为该设备生成 device_id 和鉴权 token
    3. C2 使用 device_id + token，通过 HTTP API 向 S 发送控制指令
    4. S 将指令通过 WebSocket 转发给 C1
    5. C1 在本地执行指令，将结果通过 WebSocket 返回给 S
    6. S 将结果返回给 C2 的 HTTP 响应

启动:
    python server.py [--host 0.0.0.0] [--port 8200]

C1 连接:
    python device_client.py --server http://<server>:8200

C2 控制:
    python ios_controller.py -r --server http://<server>:8200 \\
           --device <device_id> --token <token> screenshot

API 端点 (C2):
    GET  /                                    - 服务信息
    GET  /devices                             - 列出已注册设备（无需鉴权）
    GET  /devices/{id}/info                   - 设备详细信息
    GET  /devices/{id}/size                   - 屏幕尺寸
    GET  /devices/{id}/screenshot             - 截图 (PNG 或 base64)
    GET  /devices/{id}/screenshot/stream       - Stream 截图 (JPEG, 可调质量)
    POST /devices/{id}/tap                    - 点击 {"x", "y"}
    POST /devices/{id}/doubletap              - 双击 {"x", "y"}
    POST /devices/{id}/longpress              - 长按 {"x", "y", "duration"}
    POST /devices/{id}/swipe                  - 滑动 {"x1","y1","x2","y2","duration"}
    POST /devices/{id}/swipe_up               - 上滑
    POST /devices/{id}/swipe_down             - 下滑
    POST /devices/{id}/type                   - 输入文本 {"text"}
    POST /devices/{id}/home                   - Home 键
    POST /devices/{id}/volume_up              - 音量+
    POST /devices/{id}/volume_down            - 音量-
    POST /devices/{id}/launch                 - 启动应用 {"bundle_id"}

    WS   /ws/device                           - C1 WebSocket 注册端点
"""

import argparse
import asyncio
import base64
import logging
import secrets
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import (
    FastAPI,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    Depends,
    Header,
    Query,
)
from fastapi.responses import Response, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import uvicorn

logger = logging.getLogger("clawpad.server")

SERVICE_DEFAULT_PORT = 8200
COMMAND_TIMEOUT = 60  # 指令超时（秒）


# ─── 请求模型 ──────────────────────────────────────────────────────────────────

class TapRequest(BaseModel):
    x: int = Field(..., description="横坐标")
    y: int = Field(..., description="纵坐标")


class DoubleTapRequest(BaseModel):
    x: int = Field(..., description="横坐标")
    y: int = Field(..., description="纵坐标")


class LongPressRequest(BaseModel):
    x: int = Field(..., description="横坐标")
    y: int = Field(..., description="纵坐标")
    duration: float = Field(1.0, description="长按持续时间（秒）")


class SwipeRequest(BaseModel):
    x1: int = Field(..., description="起点横坐标")
    y1: int = Field(..., description="起点纵坐标")
    x2: int = Field(..., description="终点横坐标")
    y2: int = Field(..., description="终点纵坐标")
    duration: float = Field(0.5, description="滑动持续时间（秒）")


class TypeRequest(BaseModel):
    text: str = Field(..., description="要输入的文本")


class LaunchRequest(BaseModel):
    bundle_id: str = Field(..., description="应用 Bundle ID")


# ─── 设备会话 ──────────────────────────────────────────────────────────────────

class DeviceSession:
    """表示一个已注册的 C1 设备连接。"""

    def __init__(self, device_id: str, token: str, ws: WebSocket, info: dict):
        self.device_id = device_id
        self.token = token
        self.ws = ws
        self.info = info
        self.registered_at = datetime.now()
        self.pending: dict[str, asyncio.Future] = {}

    def to_public_dict(self) -> dict:
        """返回设备的公开信息（不含 token）。"""
        return {
            "device_id": self.device_id,
            "udid": self.info.get("udid"),
            "device_name": self.info.get("device_name"),
            "model": self.info.get("model"),
            "ios_version": self.info.get("ios_version"),
            "screen_width": self.info.get("screen_width"),
            "screen_height": self.info.get("screen_height"),
            "registered_at": self.registered_at.isoformat(),
            "connected": True,
        }


# ─── 设备注册表 ────────────────────────────────────────────────────────────────

class DeviceRegistry:
    """管理所有已注册的设备。线程安全。"""

    def __init__(self):
        self._devices: dict[str, DeviceSession] = {}
        self._lock = asyncio.Lock()

    async def register(self, ws: WebSocket, info: dict) -> tuple[str, str]:
        """注册设备，返回 (device_id, token)。"""
        async with self._lock:
            device_id = secrets.token_hex(4)  # 8 字符十六进制 ID
            token = secrets.token_urlsafe(32)  # 43 字符 URL-safe token
            session = DeviceSession(device_id, token, ws, info)
            self._devices[device_id] = session
            logger.info(
                f"✅ 设备注册: {device_id} "
                f"({info.get('device_name', '?')}, {info.get('model', '?')})"
            )
            return device_id, token

    async def unregister(self, device_id: str) -> None:
        """注销设备。"""
        async with self._lock:
            session = self._devices.pop(device_id, None)
            if session:
                # 取消所有待处理的请求
                for rid, future in session.pending.items():
                    if not future.done():
                        future.set_exception(
                            ConnectionError("设备已断开连接")
                        )
                session.pending.clear()
                logger.info(
                    f"👋 设备注销: {device_id} "
                    f"({session.info.get('device_name', '?')})"
                )

    def get(self, device_id: str) -> DeviceSession | None:
        return self._devices.get(device_id)

    def verify(self, device_id: str, token: str) -> bool:
        """验证 C2 提供的 token 是否匹配。"""
        session = self._devices.get(device_id)
        if session is None:
            return False
        return secrets.compare_digest(session.token, token)

    def list_devices(self) -> list[dict]:
        """列出所有已注册设备（不含 token）。"""
        return [s.to_public_dict() for s in self._devices.values()]


registry = DeviceRegistry()


# ─── 指令转发 ──────────────────────────────────────────────────────────────────

async def relay_command(
    device_id: str,
    action: str,
    params: dict | None = None,
    timeout: float = COMMAND_TIMEOUT,
) -> dict:
    """
    将控制指令转发给 C1，等待结果返回。

    1. 生成 request_id，创建 Future
    2. 通过 WebSocket 发送 command 消息给 C1
    3. 等待 C1 通过 WebSocket 返回 result
    4. 返回给调用者（HTTP handler）
    """
    session = registry.get(device_id)
    if session is None:
        raise HTTPException(status_code=404, detail="设备未找到或已离线")

    request_id = uuid4().hex[:12]
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    session.pending[request_id] = future

    try:
        await session.ws.send_json({
            "type": "command",
            "request_id": request_id,
            "action": action,
            "params": params or {},
        })
        logger.debug(f"📤 转发指令: {action} -> {device_id} (rid={request_id})")

        result = await asyncio.wait_for(future, timeout=timeout)

        if result.get("status") == "error":
            raise HTTPException(
                status_code=500,
                detail=result.get("error", "设备端执行失败"),
            )
        return result

    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=f"设备未在 {timeout}s 内响应",
        )
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))
    finally:
        session.pending.pop(request_id, None)


# ─── 鉴权依赖 ──────────────────────────────────────────────────────────────────

async def verify_auth(
    device_id: str,
    authorization: str = Header(
        ..., description="Bearer <token>", alias="Authorization"
    ),
) -> DeviceSession:
    """
    验证 C2 的鉴权信息。

    从 Authorization 头提取 Bearer token，校验是否匹配目标设备。
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authorization 格式应为: Bearer <token>",
        )
    token = authorization[7:]

    if not registry.verify(device_id, token):
        raise HTTPException(
            status_code=403,
            detail="鉴权失败: token 无效或设备不存在",
        )

    session = registry.get(device_id)
    if session is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    return session


# ─── FastAPI 应用 ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="ClawPad Server",
    description=(
        "🐾 ClawPad 中心转发服务器\n\n"
        "**架构:** C1 (设备客户端) → S (服务器) ← C2 (控制客户端)\n\n"
        "- `/ws/device` — C1 通过 WebSocket 注册设备\n"
        "- `/devices/{id}/*` — C2 通过 HTTP + Token 控制设备"
    ),
    version="2.0.0",
)

# ─── 静态文件 & Web UI ─────────────────────────────────────────────────────

STATIC_DIR = Path(__file__).parent / "static"


@app.get("/web", summary="Web 控制台", response_class=HTMLResponse)
async def web_ui():
    """返回 Web 控制台页面。"""
    index_html = STATIC_DIR / "index.html"
    if not index_html.exists():
        raise HTTPException(status_code=404, detail="Web UI 文件不存在")
    return FileResponse(index_html, media_type="text/html")


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ─── WebSocket 端点 (C1 设备客户端) ────────────────────────────────────────────

@app.websocket("/ws/device")
async def device_websocket(ws: WebSocket):
    """
    C1 设备客户端的 WebSocket 连接端点。

    协议:
        C1 -> S:  {"type": "register", "udid": "...", "device_name": "...", ...}
        S -> C1:  {"type": "registered", "device_id": "...", "token": "..."}
        S -> C1:  {"type": "command", "request_id": "...", "action": "...", "params": {...}}
        C1 -> S:  {"type": "result", "request_id": "...", "status": "ok|error", "data": {...}}
    """
    await ws.accept()
    device_id = None

    try:
        # 1. 等待注册消息（30s 超时）
        raw = await asyncio.wait_for(ws.receive_json(), timeout=30)
        if raw.get("type") != "register":
            await ws.send_json({
                "type": "error",
                "message": "首条消息必须是 register 类型",
            })
            await ws.close(code=4001)
            return

        # 2. 注册设备
        device_id, token = await registry.register(ws, raw)
        await ws.send_json({
            "type": "registered",
            "device_id": device_id,
            "token": token,
        })

        # 3. 持续接收 C1 的响应消息
        while True:
            msg = await ws.receive_json()
            msg_type = msg.get("type")

            if msg_type == "result":
                request_id = msg.get("request_id")
                session = registry.get(device_id)
                if session and request_id in session.pending:
                    future = session.pending[request_id]
                    if not future.done():
                        future.set_result(msg)
                else:
                    logger.warning(
                        f"收到未匹配的 result: rid={request_id}, device={device_id}"
                    )

            elif msg_type == "pong":
                pass  # 心跳响应

            else:
                logger.warning(f"C1 发送了未知消息类型: {msg_type}")

    except WebSocketDisconnect:
        logger.info(f"C1 连接断开: {device_id or '(未注册)'}")
    except asyncio.TimeoutError:
        logger.warning("C1 连接超时: 未在 30s 内发送注册消息")
        try:
            await ws.close(code=4002)
        except Exception:
            pass
    except Exception as e:
        logger.error(f"C1 WebSocket 异常: {e}")
    finally:
        if device_id:
            await registry.unregister(device_id)


# ─── HTTP 端点 (C2 控制客户端) ─────────────────────────────────────────────────

def _ok(action: str, **extra) -> dict:
    """构造成功响应。"""
    return {"status": "ok", "action": action, **extra}


@app.get("/", summary="服务信息")
async def root():
    devices = registry.list_devices()
    return {
        "service": "ClawPad Server",
        "version": "2.0.0",
        "architecture": "C1 (Device) -> S (Server) <- C2 (Controller)",
        "registered_devices": len(devices),
        "docs": "/docs",
    }


@app.get("/devices", summary="列出已注册设备")
async def list_devices():
    """列出所有已注册的设备（无需鉴权）。"""
    return {"devices": registry.list_devices()}


@app.get("/devices/{device_id}/info", summary="设备信息")
async def device_info(session: DeviceSession = Depends(verify_auth)):
    result = await relay_command(session.device_id, "get_device_info")
    return result.get("data", {})


@app.get("/devices/{device_id}/size", summary="屏幕尺寸")
async def screen_size(session: DeviceSession = Depends(verify_auth)):
    result = await relay_command(session.device_id, "get_screen_size")
    return result.get("data", {})


@app.get("/devices/{device_id}/screenshot", summary="截图")
async def screenshot(
    session: DeviceSession = Depends(verify_auth),
    format: str = Query("png", description="返回格式: png (图片) 或 base64 (JSON)"),
):
    result = await relay_command(
        session.device_id, "screenshot", timeout=120
    )
    data = result.get("data", {})
    image_b64 = data.get("image_base64")

    if not image_b64:
        raise HTTPException(status_code=500, detail="未收到截图数据")

    if format == "base64":
        return {"image": image_b64, "format": "base64"}
    else:
        img_bytes = base64.b64decode(image_b64)
        return Response(content=img_bytes, media_type="image/png")


@app.get("/devices/{device_id}/screenshot/stream", summary="Stream 截图")
async def screenshot_stream(
    session: DeviceSession = Depends(verify_auth),
    quality: int = Query(50, ge=1, le=100, description="JPEG 压缩质量 (1-100)"),
):
    """Stream 模式截图：不保存到磁盘，返回 JPEG 有损压缩图片。\n\n适合 Web 实时预览，通过 quality 参数控制画质与体积的平衡。"""
    result = await relay_command(
        session.device_id, "screenshot_stream",
        {"quality": quality}, timeout=120,
    )
    data = result.get("data", {})
    image_b64 = data.get("image_base64")

    if not image_b64:
        raise HTTPException(status_code=500, detail="未收到截图数据")

    img_bytes = base64.b64decode(image_b64)
    return Response(
        content=img_bytes,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/devices/{device_id}/tap", summary="点击")
async def tap(req: TapRequest, session: DeviceSession = Depends(verify_auth)):
    await relay_command(session.device_id, "tap", {"x": req.x, "y": req.y})
    return _ok("tap", x=req.x, y=req.y)


@app.post("/devices/{device_id}/doubletap", summary="双击")
async def double_tap(
    req: DoubleTapRequest, session: DeviceSession = Depends(verify_auth)
):
    await relay_command(
        session.device_id, "double_tap", {"x": req.x, "y": req.y}
    )
    return _ok("doubletap", x=req.x, y=req.y)


@app.post("/devices/{device_id}/longpress", summary="长按")
async def long_press(
    req: LongPressRequest, session: DeviceSession = Depends(verify_auth)
):
    await relay_command(
        session.device_id,
        "long_press",
        {"x": req.x, "y": req.y, "duration": req.duration},
    )
    return _ok("longpress", x=req.x, y=req.y, duration=req.duration)


@app.post("/devices/{device_id}/swipe", summary="滑动")
async def swipe(
    req: SwipeRequest, session: DeviceSession = Depends(verify_auth)
):
    await relay_command(
        session.device_id,
        "swipe",
        {
            "x1": req.x1, "y1": req.y1,
            "x2": req.x2, "y2": req.y2,
            "duration": req.duration,
        },
    )
    return _ok("swipe", x1=req.x1, y1=req.y1, x2=req.x2, y2=req.y2)


@app.post("/devices/{device_id}/swipe_up", summary="上滑")
async def swipe_up(session: DeviceSession = Depends(verify_auth)):
    await relay_command(session.device_id, "swipe_up")
    return _ok("swipe_up")


@app.post("/devices/{device_id}/swipe_down", summary="下滑")
async def swipe_down(session: DeviceSession = Depends(verify_auth)):
    await relay_command(session.device_id, "swipe_down")
    return _ok("swipe_down")


@app.post("/devices/{device_id}/type", summary="输入文本")
async def type_text(
    req: TypeRequest, session: DeviceSession = Depends(verify_auth)
):
    await relay_command(session.device_id, "type_text", {"text": req.text})
    return _ok("type", text=req.text)


@app.post("/devices/{device_id}/home", summary="Home 键")
async def press_home(session: DeviceSession = Depends(verify_auth)):
    await relay_command(session.device_id, "press_home")
    return _ok("home")


@app.post("/devices/{device_id}/volume_up", summary="音量+")
async def volume_up(session: DeviceSession = Depends(verify_auth)):
    await relay_command(session.device_id, "press_volume_up")
    return _ok("volume_up")


@app.post("/devices/{device_id}/volume_down", summary="音量-")
async def volume_down(session: DeviceSession = Depends(verify_auth)):
    await relay_command(session.device_id, "press_volume_down")
    return _ok("volume_down")


@app.post("/devices/{device_id}/launch", summary="启动应用")
async def launch_app(
    req: LaunchRequest, session: DeviceSession = Depends(verify_auth)
):
    await relay_command(
        session.device_id, "launch_app", {"bundle_id": req.bundle_id}
    )
    return _ok("launch", bundle_id=req.bundle_id)


# ─── 服务入口 ──────────────────────────────────────────────────────────────────

def start_server(host: str = "0.0.0.0", port: int = SERVICE_DEFAULT_PORT):
    """启动 ClawPad 中心转发服务器。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║                  🐾 ClawPad Server v2.0                      ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  架构:  C1 (设备) ──WS──> S (本服务) <──HTTP── C2 (控制端)   ║
║                                                              ║
║  服务地址:  http://{host}:{port}                              ║
║  API 文档:  http://{host}:{port}/docs                         ║
║                                                              ║
║  C1 注册设备:                                                ║
║    python device_client.py --server http://<IP>:{port:<12} ║
║                                                              ║
║  C2 控制设备:                                                ║
║    python ios_controller.py -r \\                             ║
║      --server http://<IP>:{port:<6} \\                           ║
║      --device <DEVICE_ID> --token <TOKEN> \\                  ║
║      screenshot                                              ║
║                                                              ║
║  按 Ctrl+C 停止服务                                          ║
╚══════════════════════════════════════════════════════════════╝
""")

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="ClawPad Server - 中心转发服务器",
    )
    parser.add_argument(
        "--host", default="0.0.0.0", help="监听地址 (默认: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=SERVICE_DEFAULT_PORT,
        help=f"服务端口 (默认: {SERVICE_DEFAULT_PORT})",
    )

    args = parser.parse_args()
    start_server(args.host, args.port)
