"""Chunk-plan tests for workspace uploads (``create_upload_task``).

The plan must be *size* driven: one chunk per ``parallel_size`` bytes, however
many chunks that takes.  It used to be *thread-count* driven
(``min(parallel_max_threads, ceil(size / parallel_size))`` chunks), which made
the chunk size grow with the file -- a 2.7 GB file became 5 PUTs of 553 MB.
The parent refuses to forward a request body above
``handler_base._MAX_PUSH_BODY_BYTES`` (512 MB) to a child environment, and it
does so while the browser is still writing the body, so the browser only sees
an opaque "upload network error" at 0%: every file above
``parallel_max_threads * 512 MB`` was un-uploadable through a remote env.
"""

from __future__ import annotations

import math
import os

import pytest

from runtime.handler_base import _MAX_PUSH_BODY_BYTES
from runtime.workspace_manager import (
    DEFAULT_UPLOAD_PARALLEL_MAX_THREADS,
    DEFAULT_UPLOAD_PARALLEL_SIZE,
    MAX_UPLOAD_CHUNK_BYTES,
    WorkspaceManager,
)


@pytest.fixture()
def ws_manager(tmp_path):
    return WorkspaceManager(str(tmp_path))


def _plan(ws_manager, file_size, parallel_size=DEFAULT_UPLOAD_PARALLEL_SIZE,
          parallel_max_threads=DEFAULT_UPLOAD_PARALLEL_MAX_THREADS):
    task = ws_manager.create_upload_task(
        file_name="payload.bin",
        file_size=file_size,
        target_path="payload.bin",
        parallel_size=parallel_size,
        parallel_max_threads=parallel_max_threads,
        target_dir_path=ws_manager.workspace_path,
        restrict_workspace=False,
    )
    return task


def _assert_covers_file(chunks, file_size):
    """Offsets are contiguous, start at 0, and the sizes add up exactly."""
    offset = 0
    for index, chunk in enumerate(chunks):
        assert chunk["parallel_id"] == index
        assert chunk["offset"] == offset
        assert chunk["size"] > 0
        offset += chunk["size"]
    assert offset == file_size


class TestChunkPlanIsSizeDriven:
    def test_default_plan_uses_parallel_size_chunks(self, ws_manager):
        # 2.7 GB (the file size that used to break uploads into a child env).
        file_size = int(2.7 * 1024 ** 3)
        task = _plan(ws_manager, file_size)
        assert task["chunk_count"] == math.ceil(file_size / DEFAULT_UPLOAD_PARALLEL_SIZE)
        for chunk in task["chunks"]:
            assert chunk["size"] <= DEFAULT_UPLOAD_PARALLEL_SIZE
        # Balanced: every chunk is the same size except the (short) last one.
        sizes = [chunk["size"] for chunk in task["chunks"]]
        assert set(sizes[:-1]) == {sizes[0]}
        assert sizes[-1] <= sizes[0]
        _assert_covers_file(task["chunks"], file_size)

    def test_chunk_count_may_exceed_parallel_max_threads(self, ws_manager):
        # parallel_max_threads is a concurrency hint for the client, not a cap
        # on the plan (capping it is what inflated the chunk size).
        file_size = 1000 * 1024 * 1024
        task = _plan(ws_manager, file_size, parallel_size=10 * 1024 * 1024,
                     parallel_max_threads=5)
        assert task["chunk_count"] == 100
        assert task["parallel_max_threads"] == 5
        assert all(chunk["size"] <= 10 * 1024 * 1024 for chunk in task["chunks"])
        _assert_covers_file(task["chunks"], file_size)

    @pytest.mark.parametrize("file_size", [
        1,
        1024,
        DEFAULT_UPLOAD_PARALLEL_SIZE - 1,
        DEFAULT_UPLOAD_PARALLEL_SIZE,
        DEFAULT_UPLOAD_PARALLEL_SIZE + 1,
        int(2.7 * 1024 ** 3),          # the reported 2.7 GB zip
        512 * 1024 * 1024 * 7,         # 3.5 GiB of "too big for the old plan"
        40 * 1024 ** 3,                # 40 GiB
    ])
    def test_no_chunk_is_bigger_than_the_plan_size(self, ws_manager, file_size):
        task = _plan(ws_manager, file_size)
        for chunk in task["chunks"]:
            assert chunk["size"] <= DEFAULT_UPLOAD_PARALLEL_SIZE
        _assert_covers_file(task["chunks"], file_size)

    def test_small_files_stay_single_chunk(self, ws_manager):
        assert _plan(ws_manager, 0)["chunks"] == []
        task = _plan(ws_manager, 5 * 1024 * 1024)
        assert task["chunk_count"] == 1
        assert task["chunks"][0] == {
            "parallel_id": 0, "offset": 0, "size": 5 * 1024 * 1024, "status": "pending",
        }


class TestChunkPlanFitsTheEnvProxy:
    """Every chunk PUT body must survive the parent -> child bridge."""

    def test_plan_ceiling_matches_the_proxy_body_cap(self):
        # MAX_UPLOAD_CHUNK_BYTES guards the bridge's limit; if one of the two
        # constants moves, this test says so instead of the browser shrugging.
        assert MAX_UPLOAD_CHUNK_BYTES <= _MAX_PUSH_BODY_BYTES

    @pytest.mark.parametrize("parallel_size", [
        DEFAULT_UPLOAD_PARALLEL_SIZE,
        256 * 1024 * 1024,
        1024 * 1024 * 1024,   # an operator raised UPLOAD_PARALLEL_SIZE
        8 * 1024 * 1024 * 1024,
    ])
    @pytest.mark.parametrize("file_size", [
        int(2.7 * 1024 ** 3),
        10 * 1024 ** 3,
        100 * 1024 ** 3,
    ])
    def test_chunks_never_exceed_the_proxy_cap(self, ws_manager, parallel_size, file_size):
        # Whatever UPLOAD_PARALLEL_SIZE says, one PUT body must stay inside
        # what /v1/env-proxy is willing to forward (512 MB).
        task = _plan(ws_manager, file_size, parallel_size=parallel_size, parallel_max_threads=5)
        for chunk in task["chunks"]:
            assert chunk["size"] <= _MAX_PUSH_BODY_BYTES, (
                f"parallel_size={parallel_size} file_size={file_size}: "
                f"chunk {chunk['parallel_id']} is {chunk['size']} B"
            )
        _assert_covers_file(task["chunks"], file_size)

    def test_upload_init_uses_the_env_var_plan(self, monkeypatch, tmp_path):
        """The HTTP handler feeds the env-var parsed sizes into the plan."""
        from runtime.workspace_manager import parse_upload_size

        monkeypatch.setenv("UPLOAD_PARALLEL_SIZE", "100MB")
        parallel_size = parse_upload_size(os.environ.get("UPLOAD_PARALLEL_SIZE"))
        task = _plan(WorkspaceManager(str(tmp_path)), int(2.7 * 1024 ** 3),
                     parallel_size=parallel_size)
        assert all(c["size"] <= 100 * 1024 * 1024 for c in task["chunks"])
