from datetime import datetime, timedelta

import pytest

from live.instance_lock import (InstanceLock, LockHeld, MachineNotAllowed,
                                check_allowed_machine)

T0 = datetime(2026, 11, 9, 8, 30)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make(path, machine="PICHU", pid=100, clock=None, alive=lambda pid: True, stale=120):
    return InstanceLock(path, mode="paper", machine=machine, pid=pid, stale_after_s=stale,
                        now=clock or Clock(T0), is_alive=alive)


def test_allowed_machine_checks():
    assert check_allowed_machine("PICHU", machine="pichu") == "pichu"
    with pytest.raises(MachineNotAllowed, match="Chưa đặt"):
        check_allowed_machine(None, machine="PICHU")
    with pytest.raises(MachineNotAllowed, match="không được phép"):
        check_allowed_machine("PICHU", machine="CFA-BINHLD-LT")


def test_acquire_release_cycle(tmp_path):
    lock = make(tmp_path / "bot.lock")
    with lock:
        assert lock.held and lock.holder().pid == 100
    assert not (tmp_path / "bot.lock").exists()


def test_second_live_process_rejected(tmp_path):
    p = tmp_path / "bot.lock"
    make(p, pid=100).acquire()
    with pytest.raises(LockHeld, match="pid 100"):
        make(p, pid=200).acquire()


def test_dead_pid_on_same_machine_is_taken_over(tmp_path):
    p = tmp_path / "bot.lock"
    make(p, pid=100).acquire()
    newcomer = make(p, pid=200, alive=lambda pid: pid != 100)
    newcomer.acquire()
    assert newcomer.holder().pid == 200


def test_other_machine_fresh_heartbeat_never_taken(tmp_path):
    p = tmp_path / "bot.lock"
    clock = Clock(T0)
    make(p, machine="PICHU", clock=clock).acquire()
    clock.t = T0 + timedelta(seconds=60)
    with pytest.raises(LockHeld):
        # máy khác không kiểm được pid ⇒ chỉ tin heartbeat
        make(p, machine="LAPTOP", pid=5, clock=clock, alive=lambda pid: False).acquire()


def test_stale_heartbeat_taken_over(tmp_path):
    p = tmp_path / "bot.lock"
    clock = Clock(T0)
    make(p, machine="PICHU", clock=clock).acquire()
    clock.t = T0 + timedelta(minutes=10)
    lock = make(p, machine="LAPTOP", pid=5, clock=clock)
    lock.acquire()
    assert lock.holder().machine == "LAPTOP"


def test_heartbeat_refreshes_and_detects_theft(tmp_path):
    p = tmp_path / "bot.lock"
    clock = Clock(T0)
    owner = make(p, clock=clock).acquire()
    clock.t = T0 + timedelta(seconds=30)
    owner.heartbeat()
    info = owner.holder()
    assert info.heartbeat_at == clock.t.isoformat(timespec="seconds")
    assert info.started_at == T0.isoformat(timespec="seconds")
    # phiên khác chiếm (vd người dùng xoá file khoá bằng tay)
    p.unlink()
    make(p, pid=999, clock=clock).acquire()
    with pytest.raises(LockHeld, match="lệnh đôi"):
        owner.heartbeat()
    assert not owner.held


def test_release_does_not_delete_someone_elses_lock(tmp_path):
    p = tmp_path / "bot.lock"
    a = make(p, pid=1).acquire()
    p.unlink()
    make(p, pid=2).acquire()
    a.release()
    assert p.exists() and make(p).holder().pid == 2


def test_corrupt_lock_file_treated_as_dead(tmp_path):
    p = tmp_path / "bot.lock"
    p.write_text("không phải json", encoding="utf-8")
    lock = make(p).acquire()
    assert lock.held
