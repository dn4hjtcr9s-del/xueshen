"""RAG 导入器单元测试：验证入库参数、CLI 边界和向量序列化。"""

from __future__ import annotations

import json

import pytest

from backend.rag.artifact_loader import ArtifactRow
from backend.rag.importer import import_artifacts, prepare_chunk_parameters
from scripts.rag_import import main


class _RecordingConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, object]] = []

    def execute(self, statement, parameters=None):
        self.executed.append((str(statement), parameters))

    def begin(self):
        connection = self

        class Transaction:
            def __enter__(self):
                return connection

            def __exit__(self, exc_type, exc_value, traceback):
                return False

        return Transaction()

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


class _RecordingEngine:
    def __init__(self, connection: _RecordingConnection) -> None:
        self.connection = connection

    def connect(self) -> _RecordingConnection:
        return self.connection


def _row(*, content_role: str = "body", retrieval_weight: float = 1.0) -> ArtifactRow:
    return ArtifactRow(
        chunk_id="3350c816-192b-589f-8d96-fbb534b2d8cd",
        chunk_index=0,
        book_id="book-1",
        book_name="测试教材",
        grade_level="高中",
        section="正文",
        chapter_path=("第一章", "一元二次方程"),
        content_role=content_role,
        retrieval_weight=retrieval_weight,
        content_text="方程 $x^2 + 2x + 1 = 0$ 的判别式。",
        embedding_text="书名：测试教材\n\n方程的判别式。",
        token_count=20,
        tokenizer_id="tiktoken:cl100k_base",
        source_page_start=7,
        source_page_end=8,
        source_refs=({"source_page": 7, "block_index": 2},),
        content_hash="a" * 64,
        source_hash="b" * 64,
        embedding_input_hash="c" * 64,
        embedding=(0.1, 0.2, 0.3),
    )


def test_prepare_chunk_parameters_preserves_provenance_and_builds_indexes() -> None:
    params = prepare_chunk_parameters(_row(), corpus_id="corpus-1")

    assert params["corpus_id"] == "corpus-1"
    assert params["source_page_start"] == 7
    assert json.loads(params["source_refs_json"])[0]["block_index"] == 2
    assert "一元" in params["search_text"].split()
    assert params["formula_terms"] == ["x^2+2x+1=0"]
    assert params["embedding_literal"] == "[0.1,0.2,0.3]"


def test_prepare_chunk_parameters_keeps_answer_key_weight() -> None:
    params = prepare_chunk_parameters(
        _row(content_role="answer_key", retrieval_weight=0.65),
        corpus_id="corpus-1",
    )

    assert params["content_role"] == "answer_key"
    assert params["retrieval_weight"] == 0.65


def test_failed_import_records_zero_loaded_chunks_after_transaction_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.rag.importer as importer
    from backend.rag.importer import RAGImportError
    from backend.rag.settings import RAGSettings

    connection = _RecordingConnection()
    engine = _RecordingEngine(connection)

    def broken_rows(_bundle):
        yield _row()
        raise RuntimeError("流式读取失败")

    bundle = type("Bundle", (), {"expected_chunk_count": 2})()
    settings = RAGSettings(
        RAG_DATABASE_URL="postgresql+psycopg://rag:rag@127.0.0.1:55433/rag",
        RAG_IMPORT_BATCH_SIZE=1,
        _env_file=None,
    )
    monkeypatch.setattr(importer, "_get_existing", lambda *_args: None)
    monkeypatch.setattr(
        importer,
        "_insert_corpus_and_run",
        lambda *_args: ("corpus-1", "run-1"),
    )
    monkeypatch.setattr(importer, "iter_artifact_rows", broken_rows)
    monkeypatch.setattr(
        importer,
        "prepare_chunk_parameters",
        lambda row, *, corpus_id: {"chunk_id": row.chunk_id, "corpus_id": corpus_id},
    )

    with pytest.raises(RAGImportError, match="流式读取失败"):
        import_artifacts(bundle, settings=settings, engine=engine)

    corpus_failure_updates = [
        parameters
        for statement, parameters in connection.executed
        if "SET status = 'failed', loaded_chunk_count" in statement
    ]
    ingest_failure_updates = [
        parameters
        for statement, parameters in connection.executed
        if "SET status = 'failed', loaded_chunks = 0" in statement
    ]
    assert corpus_failure_updates == [{"corpus_id": "corpus-1"}]
    assert ingest_failure_updates == [{"run_id": "run-1", "error_detail": "流式读取失败"}]


def test_activation_failure_keeps_successfully_loaded_corpus_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.rag.importer as importer
    from backend.rag.importer import RAGImportError
    from backend.rag.settings import RAGSettings

    connection = _RecordingConnection()
    engine = _RecordingEngine(connection)
    bundle = type("Bundle", (), {"expected_chunk_count": 1})()
    settings = RAGSettings(
        RAG_DATABASE_URL="postgresql+psycopg://rag:rag@127.0.0.1:55433/rag",
        RAG_IMPORT_BATCH_SIZE=1,
        _env_file=None,
    )
    monkeypatch.setattr(importer, "_get_existing", lambda *_args: None)
    monkeypatch.setattr(
        importer,
        "_insert_corpus_and_run",
        lambda *_args: ("corpus-1", "run-1"),
    )
    monkeypatch.setattr(importer, "iter_artifact_rows", lambda _bundle: iter((_row(),)))
    monkeypatch.setattr(
        importer,
        "prepare_chunk_parameters",
        lambda row, *, corpus_id: {"chunk_id": row.chunk_id, "corpus_id": corpus_id},
    )

    def fail_activation(*_args) -> None:
        raise RuntimeError("激活失败")

    monkeypatch.setattr(importer, "_activate", fail_activation)

    with pytest.raises(RAGImportError, match="激活失败"):
        import_artifacts(bundle, settings=settings, engine=engine)

    statements = [statement for statement, _parameters in connection.executed]
    assert any("status = 'ready'" in statement for statement in statements)
    assert any("status = 'succeeded'" in statement for statement in statements)
    assert not any("status = 'failed'" in statement for statement in statements)


def test_existing_ready_corpus_activation_failure_is_reported_as_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.rag.importer as importer
    from backend.rag.importer import RAGImportError
    from backend.rag.settings import RAGSettings

    connection = _RecordingConnection()
    engine = _RecordingEngine(connection)
    bundle = type("Bundle", (), {})()
    settings = RAGSettings(
        RAG_DATABASE_URL="postgresql+psycopg://rag:rag@127.0.0.1:55433/rag",
        _env_file=None,
    )
    monkeypatch.setattr(
        importer,
        "_get_existing",
        lambda *_args: {
            "corpus_id": "corpus-1",
            "status": "ready",
            "expected_chunk_count": 1,
            "loaded_chunk_count": 1,
        },
    )

    def fail_activation(*_args) -> None:
        raise RuntimeError("激活失败")

    monkeypatch.setattr(importer, "_activate", fail_activation)

    with pytest.raises(RAGImportError, match="激活失败"):
        import_artifacts(bundle, settings=settings, engine=engine)

    assert not any(
        "status = 'failed'" in statement for statement, _parameters in connection.executed
    )


def test_rag_import_cli_requires_explicit_artifact_roots() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main([])

    assert exc_info.value.code == 2


def test_rag_import_cli_serializes_slots_result(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import scripts.rag_import as cli
    from backend.rag.importer import ImportResult

    chunk_root = tmp_path / "chunks"
    embedding_root = tmp_path / "embeddings"
    chunk_root.mkdir()
    embedding_root.mkdir()
    monkeypatch.setenv("RAG_DATABASE_URL", "postgresql+psycopg://rag:rag@127.0.0.1:55433/rag")
    monkeypatch.setattr(cli, "validate_artifacts", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        cli,
        "import_artifacts",
        lambda *args, **kwargs: ImportResult(
            corpus_id="corpus-1",
            run_id="run-1",
            status="active",
            expected_chunks=1,
            loaded_chunks=1,
        ),
    )

    exit_code = cli.main(["--chunk-root", str(chunk_root), "--embedding-root", str(embedding_root)])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["corpus_id"] == "corpus-1"


def test_rag_import_script_runs_by_file_path_from_any_cwd(tmp_path) -> None:
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "scripts" / "rag_import.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--chunk-root" in completed.stdout


def test_rag_import_cli_honors_no_activate(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.rag_import as cli
    from backend.rag.importer import ImportResult

    chunk_root = tmp_path / "chunks"
    embedding_root = tmp_path / "embeddings"
    chunk_root.mkdir()
    embedding_root.mkdir()
    monkeypatch.setenv("RAG_DATABASE_URL", "postgresql+psycopg://rag:rag@127.0.0.1:55433/rag")
    monkeypatch.setattr(cli, "validate_artifacts", lambda *args, **kwargs: object())
    captured: dict[str, object] = {}

    def fake_import(*args, **kwargs):
        captured.update(kwargs)
        return ImportResult(
            corpus_id="corpus-1",
            run_id="run-1",
            status="ready",
            expected_chunks=1,
            loaded_chunks=1,
        )

    monkeypatch.setattr(cli, "import_artifacts", fake_import)

    exit_code = cli.main(
        [
            "--chunk-root",
            str(chunk_root),
            "--embedding-root",
            str(embedding_root),
            "--no-activate",
        ]
    )

    assert exit_code == 0
    assert captured["activate"] is False
