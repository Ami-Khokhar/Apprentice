from __future__ import annotations

import os
import stat

import pytest

from apprentice.database import SQLiteDatabase


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions are not portable to Windows")
def test_database_hardens_existing_file_permissions(tmp_path) -> None:
    path = tmp_path / "apprentice.db"
    path.write_bytes(b"")
    path.chmod(0o666)

    database = SQLiteDatabase(path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with database.transaction() as connection:
        connection.execute("SELECT 1").fetchone()
        for suffix in ("-wal", "-shm"):
            sidecar = path.with_name(path.name + suffix)
            if sidecar.exists():
                assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
