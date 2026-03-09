# commands/move.py
"""The move command — velocity-function-based movement with duration.

Usage::

    move fx="500*sin(t)" fy="500*cos(t)" duration=5
    move fx="300" fy="0" fz="100*sin(t)" duration=3 dt=0.05
    move stop
    move status

fx, fy, fz are velocity expressions evaluated with t
(seconds since start).  Each tick the position is integrated::
    loc += vel(t) * dt

When the duration expires, a few stop packets (zero-velocity, final position) are sent automatically.
"""
from __future__ import annotations

import ast
import math
import socket
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from commands.actors import (
    get_runtime_cmd_state,
    resolve_class_name,
    resolve_rpc_field,
    try_auto_ack_possession,
)
from commands.base import CommandContext, cmd_log, register, register_tick
from commands.movement import build_move_rpc_payload
from core.names.ename import EName
from net.rpc.sender import build_actor_rpc_packet
from net.state.game_state import get_game_state

if TYPE_CHECKING:
    from net.connection import NetConnection


# ---------------------------------------------------------------------------
# Safe math evaluation
# ---------------------------------------------------------------------------

ExpressionFn = Callable[[float], float]

_MATH_FUNCS: dict[str, Callable[..., float]] = {
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "atan2": math.atan2,
    "sqrt": math.sqrt,
    "abs": abs,
    "pow": pow,
    "min": min,
    "max": max,
    "exp": math.exp,
    "log": math.log,
    "floor": math.floor,
    "ceil": math.ceil,
}
_MATH_CONSTS: dict[str, float] = {
    "pi": math.pi,
    "e": math.e,
}
_EXPR_BASE_NS: dict[str, object] = {**_MATH_FUNCS, **_MATH_CONSTS}
_ALLOWED_NAMES: set[str] = set(_EXPR_BASE_NS) | {"t"}
_ALLOWED_AST_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.UAdd,
    ast.USub,
)
_ALLOWED_OPTIONS = {
    "fx",
    "fy",
    "fz",
    "duration",
    "dur",
    "dt",
    "stop",
    "rpc",
    "pitch",
    "yaw",
    "roll",
    "mode",
    "ch",
}
_MAX_EXPR_LEN = 128


def _compile_expr(expr: str) -> ExpressionFn:
    src = expr.strip()
    if not src:
        raise ValueError("expression is empty")
    if len(src) > _MAX_EXPR_LEN:
        raise ValueError(f"expression is too long (max {_MAX_EXPR_LEN} chars)")

    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"syntax error: {exc.msg}") from None

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_AST_NODES):
            raise ValueError(f"unsupported syntax: {node.__class__.__name__}")
        if isinstance(node, ast.Name) and node.id not in _ALLOWED_NAMES:
            raise ValueError(f"unsupported name: {node.id!r}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _MATH_FUNCS:
                raise ValueError("only whitelisted math functions are allowed")
            if node.keywords:
                raise ValueError("keyword arguments are not supported")
        if isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"unsupported constant: {value!r}")

    code = compile(tree, "<move-expr>", "eval")

    def _evaluate(t: float) -> float:
        value = float(
            eval(
                code,
                {"__builtins__": {}},
                {**_EXPR_BASE_NS, "t": float(t)},
            )
        )
        if not math.isfinite(value):
            raise ValueError(f"expression produced non-finite value: {value}")
        return value

    return _evaluate


def _parse_float_option(opts: dict[str, str], key: str, default: float) -> float:
    raw = opts.get(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{key} must be a number: {raw!r}") from None


def _parse_int_option(opts: dict[str, str], key: str, default: int) -> int:
    raw = opts.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{key} must be an integer: {raw!r}") from None


# ---------------------------------------------------------------------------
# Move state
# ---------------------------------------------------------------------------

@dataclass
class MoveState:
    active: bool = False
    pending_stop: int = 0
    stop_count: int = 3
    fx: str = "0"
    fy: str = "0"
    fz: str = "0"
    fx_eval: ExpressionFn | None = None
    fy_eval: ExpressionFn | None = None
    fz_eval: ExpressionFn | None = None
    duration: float = 5.0
    dt: float = 0.033
    elapsed: float = 0.0
    ts: float = 1.0
    loc_x: float = 0.0
    loc_y: float = 0.0
    loc_z: float = 0.0
    ch_index: int = 0
    rpc_variant: str = "packed"
    field_index: int = 0
    field_max: int = 0
    pitch: float = 0.0
    yaw: float = 0.0
    roll: float = 0.0
    mode: int = 1
    next_send_at: float = 0.0
    last_send_time: float = 0.0


def _get_move_state(conn: NetConnection) -> MoveState:
    state = get_runtime_cmd_state(conn)
    ms = state.get("move")
    if not isinstance(ms, MoveState):
        ms = MoveState()
        state["move"] = ms
    return ms


def _find_pawn_channel(conn: NetConnection) -> tuple[int, str] | None:
    """Auto-detect a channel whose class supports movement RPCs."""
    for idx, ch in enumerate(conn.channels):
        if ch is None or idx == 0 or getattr(ch, "ch_name", None) != EName.Actor:
            continue
        class_name = resolve_class_name(conn, ch)
        if not class_name:
            continue
        if resolve_rpc_field(class_name, "ServerMovePacked") is not None:
            return idx, class_name
        if resolve_rpc_field(class_name, "ServerMoveNoBase") is not None:
            return idx, class_name
        if resolve_rpc_field(class_name, "ServerMoveOld") is not None:
            return idx, class_name
    return None


def _get_loc_from_channel(conn: NetConnection, ch_index: int) -> tuple[float, float, float]:
    ch = conn.channels[ch_index]
    if ch is not None:
        sd = getattr(ch, "_spawn_data", None)
        new_actor = getattr(sd, "new_actor", None) if sd is not None else None
        loc = getattr(new_actor, "location", None)
        if loc is not None:
            try:
                return float(loc.x), float(loc.y), float(loc.z)
            except (AttributeError, TypeError, ValueError):
                pass
    return 0.0, 0.0, 0.0


_RPC_MAP = {"nobase": "ServerMoveNoBase", "old": "ServerMoveOld", "packed": "ServerMovePacked"}


def _resolve_move_rpc(class_name: str, variant: str):
    rpc_name = _RPC_MAP.get(variant, "ServerMovePacked")
    resolved = resolve_rpc_field(class_name, rpc_name)
    if resolved is None:
        raise ValueError(f"{rpc_name} not found for class: {class_name}")
    return rpc_name, resolved


# ---------------------------------------------------------------------------
# Packet send helper
# ---------------------------------------------------------------------------

def _send_move_packet(
    ms: MoveState,
    conn: NetConnection,
    sock: socket.socket,
    server_addr: tuple[str, int],
    vx: float, vy: float, vz: float,
) -> None:
    payload, bits = build_move_rpc_payload(
        rpc_variant=ms.rpc_variant,
        ts=ms.ts,
        accel={"x": vx, "y": vy, "z": vz},
        loc={"x": ms.loc_x, "y": ms.loc_y, "z": ms.loc_z},
        pitch=ms.pitch, yaw=ms.yaw, roll=ms.roll,
        movement_mode=ms.mode,
    )
    pkt = build_actor_rpc_packet(
        conn=conn, ch_index=ms.ch_index,
        field_index=ms.field_index, field_max=ms.field_max,
        rpc_payload=payload, rpc_payload_bits=bits, reliable=False,
    )
    sock.sendto(pkt, server_addr)


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------

def _cmd_move(ctx: CommandContext, tokens: list[str]) -> tuple[bool, bool]:
    conn = ctx.conn
    ms = _get_move_state(conn)
    sent_packet = False

    # -- sub-commands --------------------------------------------------------
    if len(tokens) >= 2:
        sub = tokens[1].strip().lower()
        if sub in {"stop", "off"}:
            if ms.active:
                ms.active = False
                ms.pending_stop = ms.stop_count
                ms.next_send_at = time.perf_counter()
                cmd_log("[CMD] move stopped")
            return False, False
        if sub == "status":
            cmd_log(
                f"[CMD] move active={ms.active} "
                f"pending_stop={ms.pending_stop} "
                f"elapsed={ms.elapsed:.2f}/{ms.duration:.1f}s "
                f"loc=({ms.loc_x:.1f},{ms.loc_y:.1f},{ms.loc_z:.1f})"
            )
            return False, False

    # -- parse options -------------------------------------------------------
    opts: dict[str, str] = {}
    for token in tokens[1:]:
        if "=" not in token:
            raise ValueError(f"Invalid option format: {token!r} (expected key=value)")
        key, value = token.split("=", 1)
        key = key.strip().lower()
        if key not in _ALLOWED_OPTIONS:
            raise ValueError(f"Unknown option: {key!r}")
        opts[key] = value

    fx = opts.get("fx", "0")
    fy = opts.get("fy", "0")
    fz = opts.get("fz", "0")
    duration = _parse_float_option(opts, "duration", 5.0)
    if "duration" not in opts:
        duration = _parse_float_option(opts, "dur", 5.0)
    dt = _parse_float_option(opts, "dt", 0.033)
    stop_count = _parse_int_option(opts, "stop", 3)
    rpc_variant = opts.get("rpc", "packed").strip().lower()
    if rpc_variant in {"default", "normal"}:
        rpc_variant = "packed"
    if rpc_variant not in _RPC_MAP:
        raise ValueError(f"rpc must be one of: {', '.join(_RPC_MAP)}")
    pitch = _parse_float_option(opts, "pitch", 0.0)
    yaw = _parse_float_option(opts, "yaw", 0.0)
    roll = _parse_float_option(opts, "roll", 0.0)
    mode = _parse_int_option(opts, "mode", 1) & 0xFF

    if duration <= 0:
        raise ValueError("duration must be > 0")
    if dt <= 0:
        raise ValueError("dt must be > 0")
    if stop_count < 0:
        raise ValueError("stop must be >= 0")

    # validate expressions
    compiled_exprs: dict[str, ExpressionFn] = {}
    for name, expr in [("fx", fx), ("fy", fy), ("fz", fz)]:
        try:
            fn = _compile_expr(expr)
            fn(0.0)
            compiled_exprs[name] = fn
        except Exception as e:
            raise ValueError(f"Invalid expression {name}={expr!r}: {e}") from None

    # -- resolve target channel ----------------------------------------------
    ch_index_str = opts.get("ch")
    class_name: str = ""
    if ch_index_str is not None:
        ch_index = _parse_int_option(opts, "ch", 0)
        if ch_index < 0 or ch_index >= len(conn.channels):
            raise ValueError(f"Channel index out of range: {ch_index}")
        if conn.channels[ch_index] is None:
            raise ValueError(f"Channel {ch_index} not open")
        class_name = resolve_class_name(conn, conn.channels[ch_index])
    else:
        result = _find_pawn_channel(conn)
        if result is None:
            raise ValueError("No pawn channel found — specify ch=<index>")
        ch_index, class_name = result

    if not class_name:
        raise ValueError("Cannot determine class for channel")

    rpc_name, (resolved_class, field_index, field_max) = _resolve_move_rpc(class_name, rpc_variant)

    # -- auto-ack possession -------------------------------------------------
    auto_ack = try_auto_ack_possession(conn, ch_index)
    if auto_ack is not None:
        ack_pkt, _, _, ack_cc, ack_pg = auto_ack
        ctx.sock.sendto(ack_pkt, ctx.server_addr)
        sent_packet = True
        cmd_log(f"[->] CMD ackpawn(auto) ({len(ack_pkt)}) | pc_ch={ack_cc} pawn_guid={ack_pg}")

    lx, ly, lz = _get_loc_from_channel(conn, ch_index)

    # -- activate loop -------------------------------------------------------
    ms.active = True
    ms.pending_stop = 0
    ms.stop_count = stop_count
    ms.fx = fx
    ms.fy = fy
    ms.fz = fz
    ms.fx_eval = compiled_exprs["fx"]
    ms.fy_eval = compiled_exprs["fy"]
    ms.fz_eval = compiled_exprs["fz"]
    ms.duration = duration
    ms.dt = dt
    ms.elapsed = 0.0
    ch = conn.channels[ch_index]
    ms.ts = time.perf_counter() - ch.created_at + 0.1
    ms.loc_x = lx
    ms.loc_y = ly
    ms.loc_z = lz
    ms.ch_index = ch_index
    ms.rpc_variant = rpc_variant
    ms.field_index = field_index
    ms.field_max = field_max
    ms.pitch = pitch
    ms.yaw = yaw
    ms.roll = roll
    ms.mode = mode
    ms.next_send_at = time.perf_counter()
    ms.last_send_time = 0.0

    cmd_log(
        f"[->] CMD move start | ch={ch_index} class={resolved_class} rpc={rpc_name} "
        f"duration={duration:.1f}s dt={dt:.3f} fx={fx!r} fy={fy!r} fz={fz!r}"
    )
    return sent_packet, False


# ---------------------------------------------------------------------------
# Tick (called from main event loop)
# ---------------------------------------------------------------------------

_INF = float('inf')


def tick_move(
    conn: NetConnection,
    sock: socket.socket,
    server_addr: tuple[str, int],
) -> tuple[bool, float]:
    ms = _get_move_state(conn)
    if not ms.active and ms.pending_stop <= 0:
        return False, _INF

    now = time.perf_counter()
    if now < ms.next_send_at:
        return False, ms.next_send_at

    real_dt = now - ms.last_send_time if ms.last_send_time > 0 else ms.dt
    ms.last_send_time = now
    ms.next_send_at = now + ms.dt

    if ms.active:
        if ms.elapsed >= ms.duration:
            ms.active = False
            ms.pending_stop = ms.stop_count
            cmd_log(f"[CMD] move finished ({ms.duration}s) — sending stop packets")
        else:
            if ms.fx_eval is None or ms.fy_eval is None or ms.fz_eval is None:
                cmd_log("[CMD] move aborted: expression evaluators not initialized")
                ms.active = False
                ms.pending_stop = 0
                return False, _INF

            try:
                vx = ms.fx_eval(ms.elapsed)
                vy = ms.fy_eval(ms.elapsed)
                vz = ms.fz_eval(ms.elapsed)
            except Exception as exc:
                cmd_log(f"[CMD] move aborted: {exc}")
                ms.active = False
                ms.pending_stop = 0
                return False, _INF

            ms.loc_x += vx * real_dt
            ms.loc_y += vy * real_dt
            ms.loc_z += vz * real_dt
            ms.ts += real_dt
            ms.elapsed += real_dt

            _send_move_packet(ms, conn, sock, server_addr, vx, vy, vz)
            return True, ms.next_send_at

    if ms.pending_stop > 0:
        ms.ts += real_dt
        _send_move_packet(ms, conn, sock, server_addr, 0.0, 0.0, 0.0)
        ms.pending_stop -= 1
        if ms.pending_stop <= 0:
            cmd_log("[CMD] move stop packets sent")
            return True, _INF
        return True, ms.next_send_at

    return False, _INF


register("move", _cmd_move)
register_tick(tick_move)
