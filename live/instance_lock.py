"""Khoá một-phiên-live (CLAUDE.md mục 3b).

Người dùng có hai máy; nếu bot chạy cùng lúc ở cả hai thì mỗi tín hiệu thành HAI lệnh.
Runner live/paper phải `acquire()` trước khi kết nối broker:

    with InstanceLock(state_dir / "bot.lock", mode="live"):
        run()

Cơ chế: file khoá tạo bằng O_CREAT|O_EXCL (nguyên tử trên cả Windows lẫn Linux), chứa
{machine, pid, mode, started_at, heartbeat_at}. Runner gọi `heartbeat()` định kỳ.
Khoá bị coi là CHẾT (được phép chiếm) khi:
- cùng máy và pid không còn sống; hoặc
- heartbeat cũ hơn `stale_after_s` (máy kia tắt/treo/ngủ).
Khoá của máy khác còn heartbeat tươi ⇒ KHÔNG BAO GIỜ tự chiếm.

Hai lớp chặn:
1. `check_allowed_machine()` — settings `live.allowed_machine` ghi đúng MỘT hostname được chạy
   bot; máy khác (kể cả laptop có cùng code) từ chối khởi động. Chặn chéo hai máy mà không cần
   thư mục dùng chung (không dựa vào Dropbox: đồng bộ không nguyên tử).
2. `InstanceLock` — file khoá trong `state_dir` cục bộ, chặn hai tiến trình trên CÙNG máy (lỗi
   hay gặp nhất: Task Scheduler khởi động bản thứ hai khi bản đầu còn chạy).
"""
from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from utils.atomic_io import write_json_atomic


class LockHeld(RuntimeError):
    """Một phiên khác đang giữ khoá."""


class MachineNotAllowed(RuntimeError):
    """Máy này không phải máy được chỉ định chạy bot."""


def check_allowed_machine(allowed: Optional[str], machine: Optional[str] = None) -> str:
    """Trả hostname hiện tại nếu được phép; ném lỗi khi chưa cấu hình hoặc sai máy.

    So sánh không phân biệt hoa thường (Windows trả COMPUTERNAME viết hoa).
    """
    current = machine or socket.gethostname()
    if not allowed:
        raise MachineNotAllowed(
            "Chưa đặt live.allowed_machine trong config/settings.yaml. "
            f"Máy này tên '{current}' — điền đúng tên máy DUY NHẤT được chạy bot.")
    if current.strip().lower() != str(allowed).strip().lower():
        raise MachineNotAllowed(
            f"Máy '{current}' không được phép chạy bot (live.allowed_machine = '{allowed}').")
    return current


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass(frozen=True)
class LockInfo:
    machine: str
    pid: int
    mode: str
    started_at: str
    heartbeat_at: str

    @classmethod
    def from_file(cls, path: Path) -> Optional["LockInfo"]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(**{k: data[k] for k in cls.__dataclass_fields__})
        except (OSError, ValueError, KeyError, TypeError):
            return None


class InstanceLock:
    def __init__(self, path: Path | str, mode: str = "paper", stale_after_s: float = 120.0,
                 machine: Optional[str] = None, pid: Optional[int] = None,
                 now: Callable[[], datetime] = datetime.now,
                 is_alive: Callable[[int], bool] = pid_alive):
        self.path = Path(path)
        self.mode = mode
        self.stale_after = timedelta(seconds=stale_after_s)
        self.machine = machine or socket.gethostname()
        self.pid = pid if pid is not None else os.getpid()
        self._now = now
        self._is_alive = is_alive
        self._held = False

    # ── kiểm tra ─────────────────────────────────────────────────────────────
    def holder(self) -> Optional[LockInfo]:
        return LockInfo.from_file(self.path) if self.path.exists() else None

    def _is_dead(self, info: Optional[LockInfo]) -> bool:
        if info is None:
            return True                       # file hỏng/không đọc được: coi như chết
        if info.machine == self.machine and not self._is_alive(info.pid):
            return True
        try:
            beat = datetime.fromisoformat(info.heartbeat_at)
        except ValueError:
            return True
        return self._now() - beat > self.stale_after

    # ── vòng đời ─────────────────────────────────────────────────────────────
    def _payload(self, started_at: Optional[str] = None) -> dict:
        now = self._now().isoformat(timespec="seconds")
        return {"machine": self.machine, "pid": self.pid, "mode": self.mode,
                "started_at": started_at or now, "heartbeat_at": now}

    def _create_exclusive(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self._payload(), fh, ensure_ascii=False)
        return True

    def acquire(self) -> "InstanceLock":
        if self._create_exclusive():
            self._held = True
            return self
        info = self.holder()
        if not self._is_dead(info):
            raise LockHeld(
                f"Bot đang chạy ở máy {info.machine} (pid {info.pid}, {info.mode}, "
                f"từ {info.started_at}, heartbeat {info.heartbeat_at}). "
                "Dừng phiên đó trước, hoặc xoá file khoá nếu chắc chắn nó đã chết: "
                f"{self.path}")
        # khoá chết: xoá rồi chiếm lại bằng O_EXCL (hai tiến trình cùng chiếm thì chỉ một thắng)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        if not self._create_exclusive():
            raise LockHeld(f"Tiến trình khác vừa chiếm khoá {self.path}")
        self._held = True
        return self

    def heartbeat(self) -> None:
        if not self._held:
            raise RuntimeError("Chưa giữ khoá")
        info = self.holder()
        if info is None or info.machine != self.machine or info.pid != self.pid:
            self._held = False
            raise LockHeld("Khoá đã bị phiên khác chiếm — dừng ngay để tránh lệnh đôi")
        write_json_atomic(self._payload(started_at=info.started_at), self.path)

    def release(self) -> None:
        if not self._held:
            return
        info = self.holder()
        if info is not None and info.machine == self.machine and info.pid == self.pid:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    def __enter__(self) -> "InstanceLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
