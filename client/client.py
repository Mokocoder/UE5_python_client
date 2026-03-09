# client.py
from __future__ import annotations

import sys
sys.dont_write_bytecode = True

import argparse
import queue
import select
import socket
import time
import traceback
from datetime import datetime

from app_config import LOCAL_NETWORK_VERSION
from commands import CommandContext, drain_commands, tick_all
from constants import SEQ_NUMBER_MASK
from dashboard import start_server as start_dashboard
from net.connection import NetConnection
from net.handlers.stateless_connect import StatelessConnectHandlerComponent
from net.packets.control import NMT
from net.state.session_state import get_session_state

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_SERVER_IP = "127.0.0.1"
DEFAULT_SERVER_PORT = 7777
MAX_WAIT = 5.0
KEEPALIVE_INTERVAL = 10.0
COMMAND_HTTP_HOST = "127.0.0.1"
COMMAND_HTTP_PORT = 18765


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

class Logger:
    __slots__ = ("terminal", "log")

    def __init__(self, filename: str):
        self.terminal = sys.stdout
        self.log = open(filename, "w", encoding="utf-8")

    def write(self, message: str) -> None:
        self.terminal.write(message)
        self.log.write(message)
        self.log.flush()

    def flush(self) -> None:
        self.terminal.flush()
        self.log.flush()

    def close(self) -> None:
        try:
            self.log.close()
        except Exception:
            pass


_logger: Logger | None = None


def configure_output_logging() -> None:
    global _logger
    if _logger is not None:
        return
    log_file = f"client_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    _logger = Logger(log_file)
    sys.stdout = _logger
    sys.stderr = _logger


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def _recv_blocking(sock: socket.socket, timeout: float = 5.0) -> tuple[bytes, tuple]:
    readable, _, _ = select.select([sock], [], [], timeout)
    if not readable:
        raise TimeoutError("No response from server")
    return sock.recvfrom(4096)


def _send_disconnect(conn: NetConnection, sock: socket.socket, server_addr: tuple) -> None:
    print("\n[INFO] Disconnecting...")
    try:
        pkt = conn.create_disconnect_packet()
        sock.sendto(pkt, server_addr)
        print(f"[->] Disconnect         ({len(pkt)}) {pkt.hex()}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------

def _handshake(
    sock: socket.socket,
    server_addr: tuple,
    stateless: StatelessConnectHandlerComponent,
) -> NetConnection:
    pkt = stateless.get_initial_packet()
    print(f"[->] Init               ({len(pkt)}) {pkt.hex()}")
    sock.sendto(pkt, server_addr)
    print(f"[INFO] Client local port: {sock.getsockname()[1]}")

    data, _ = _recv_blocking(sock)
    print(f"[<-] Challenge          ({len(data)}) {data.hex()}")
    challenge = stateless.parse_handshake_packet(data)

    if challenge.LocalNetworkVersion != 0:
        print(f"[INFO] Server NetworkVersion: {challenge.LocalNetworkVersion}")
        if challenge.LocalNetworkVersion != LOCAL_NETWORK_VERSION:
            print(f"[WARN] Version mismatch! Client: {LOCAL_NETWORK_VERSION}")
        stateless.LocalNetworkVersion = challenge.LocalNetworkVersion

    pkt = stateless.get_challenge_response_packet(challenge)
    print(f"[->] Challenge Response ({len(pkt)}) {pkt.hex()}")
    sock.sendto(pkt, server_addr)

    data, _ = _recv_blocking(sock)
    print(f"[<-] Challenge Ack      ({len(data)}) {data.hex()}")
    ack = stateless.parse_handshake_packet(data)

    stateless.CachedClientID = ack.CachedClientID
    print(f"[INFO] CachedClientID: {ack.CachedClientID}")

    in_seq = int.from_bytes(ack.Cookie[0:2], "little") & SEQ_NUMBER_MASK
    out_seq = int.from_bytes(ack.Cookie[2:4], "little") & SEQ_NUMBER_MASK
    print(f"[INFO] InSeq: {in_seq}, OutSeq: {out_seq}")

    net_version = challenge.LocalNetworkVersion or LOCAL_NETWORK_VERSION
    conn = NetConnection(
        cached_client_id=ack.CachedClientID,
        initial_in_seq=in_seq,
        initial_out_seq=out_seq,
        local_network_version=net_version,
    )
    conn.set_handlers([stateless])
    return conn


# ---------------------------------------------------------------------------
# Event loop
# ---------------------------------------------------------------------------

def _run_event_loop(
    conn: NetConnection,
    sock: socket.socket,
    server_addr: tuple,
    command_queue: queue.Queue[str],
) -> None:
    ctx = CommandContext(conn=conn, sock=sock, server_addr=server_addr)
    last_send = time.time()
    next_tick = float('inf')

    while True:
        sent, should_disconnect = drain_commands(command_queue, ctx)
        if sent:
            last_send = time.time()
        if should_disconnect:
            return

        keepalive_in = last_send + KEEPALIVE_INTERVAL - time.time()
        tick_in = next_tick - time.perf_counter()
        wait = max(0.0, min(tick_in, keepalive_in, MAX_WAIT))

        readable, _, _ = select.select([sock], [], [], wait)

        if readable:
            data, addr = sock.recvfrom(4096)
            print(f"[<-] Server             ({len(data)}) {data.hex()}")

            for pkt in conn.received_raw_packet(data):
                print(f"[->] Response           ({len(pkt)}) {pkt.hex()}")
                sock.sendto(pkt, addr)
                last_send = time.time()

            if conn.b_closed:
                print(f"[INFO] Server closed connection: {conn.close_reason or 'no reason'}")
                return

        tick_sent, next_tick = tick_all(conn, sock, server_addr)
        if tick_sent:
            last_send = time.time()

        if time.time() - last_send >= KEEPALIVE_INTERVAL:
            keepalive = conn.create_empty_packet(80)
            sock.sendto(keepalive, server_addr)
            last_send = time.time()
            print("[->] Keepalive")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(server_ip: str, server_port: int, *, dashboard: bool = False) -> None:
    print("=" * 60)
    print(f"Server: {server_ip}:{server_port}")
    print("=" * 60)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    server_addr = (server_ip, server_port)

    stateless = StatelessConnectHandlerComponent(
        CachedClientID=0,
        LocalNetworkVersion=LOCAL_NETWORK_VERSION,
    )

    command_http_server = None
    try:
        conn = _handshake(sock, server_addr, stateless)
        get_session_state(conn).login_params = {"URL": "?Name=Player"}

        pkt = NMT.Hello.Get(conn)
        print(f"[->] NMT_Hello          ({len(pkt)}) {pkt.hex()}")
        sock.sendto(pkt, server_addr)

        print("\n" + "=" * 60)
        print("Connected! Listening...")
        print("=" * 60 + "\n")

        command_queue: queue.Queue[str] = queue.Queue()
        if dashboard:
            command_http_server = start_dashboard(command_queue, COMMAND_HTTP_HOST, COMMAND_HTTP_PORT)

        _run_event_loop(conn, sock, server_addr, command_queue)

    except KeyboardInterrupt:
        _send_disconnect(conn, sock, server_addr)

    except Exception:
        traceback.print_exc()

    finally:
        if command_http_server is not None:
            try:
                command_http_server.shutdown()
                command_http_server.server_close()
            except Exception:
                pass
        sock.close()
        print("[INFO] Connection closed")
        if _logger is not None:
            _logger.close()


if __name__ == "__main__":
    configure_output_logging()
    p = argparse.ArgumentParser()
    p.add_argument("--ip", default=DEFAULT_SERVER_IP)
    p.add_argument("--port", type=int, default=DEFAULT_SERVER_PORT)
    p.add_argument("--dashboard", action="store_true", help="enable web dashboard")
    a = p.parse_args()
    main(server_ip=a.ip, server_port=a.port, dashboard=a.dashboard)
