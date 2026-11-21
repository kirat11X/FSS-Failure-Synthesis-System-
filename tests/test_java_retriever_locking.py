import os

from bridge.java_retriever import _lock_sidecar_file, _unlock_sidecar_file


def test_sidecar_lock_can_be_acquired_and_released(tmp_path):
    lock_path = tmp_path / "store.lock"
    with lock_path.open("a+b") as lock_file:
        _lock_sidecar_file(lock_file)
        try:
            if os.name == "nt":
                assert lock_path.stat().st_size >= 1
        finally:
            _unlock_sidecar_file(lock_file)
