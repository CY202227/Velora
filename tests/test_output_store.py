import pytest
from server.core.execution.artifacts import OutputStore
from server.core.runtime_lease import RuntimeLease


def test_output_pages_session_binding_and_quota(tmp_path):
    store = OutputStore(tmp_path, 1000)
    store.put("e", "s", {"stdout": "abcdef", "stderr": "", "stdout_truncated": True})
    page = store.read("e", "s", offset=2, limit=2)
    assert page["content"] == "cd" and page["next_offset"] == 4 and page["capture_truncated"]
    with pytest.raises(ValueError, match="another session"):
        store.read("e", "other")
    with pytest.raises(ValueError, match="already exists"):
        store.put("e", "s", {})
    with pytest.raises(ValueError, match="budget"):
        store.put("large", "s", {"stdout": "x" * 1000})


def test_runtime_lease_refuses_second_owner_and_releases(tmp_path):
    database = "sqlite+aiosqlite:///" + (tmp_path / "runtime.db").as_posix()
    with RuntimeLease(database, tmp_path):
        with pytest.raises(RuntimeError, match="Another service"):
            with RuntimeLease(database, tmp_path):
                pytest.fail("second owner acquired runtime")
    with RuntimeLease(database, tmp_path):
        pass
