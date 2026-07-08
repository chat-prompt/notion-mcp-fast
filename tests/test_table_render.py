"""Unit tests for table block rendering (reader._render_table)."""

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from notion_mcp_fast.reader import NotionLocalReader


def _make_conn(rows):
    """Build an in-memory SQLite with a minimal `block` table.

    rows: list of (id, properties_dict).
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE block (id TEXT PRIMARY KEY, properties TEXT, alive INTEGER)"
    )
    for rid, props in rows:
        conn.execute(
            "INSERT INTO block (id, properties, alive) VALUES (?, ?, 1)",
            (rid, json.dumps(props, ensure_ascii=False)),
        )
    conn.commit()
    return conn


def _reader():
    # __init__ only stores the path; no connection is opened here.
    return NotionLocalReader(db_path=":memory:")


def test_tr01_header_table():
    """TR-01: 헤더 있는 기본 표 (한글 셀 포함)"""
    conn = _make_conn([
        ("h", {"c1": [["상황"]], "c2": [["처리"]]}),
        ("r1", {"c1": [["카드 소진"]], "c2": [["경고 출력"]]}),
    ])
    fmt = json.dumps({
        "table_block_column_order": ["c1", "c2"],
        "table_block_column_header": True,
    })
    result = _reader()._render_table(conn, ["h", "r1"], fmt)
    expected = (
        "| 상황 | 처리 |\n"
        "| --- | --- |\n"
        "| 카드 소진 | 경고 출력 |"
    )
    assert result == expected, f"got:\n{result}"
    print("[PASS] TR-01 - 헤더 있는 기본 표")


def test_tr02_column_order_respected():
    """TR-02: 컬럼 순서(format)를 따른다"""
    conn = _make_conn([
        ("h", {"c1": [["상황"]], "c2": [["처리"]]}),
        ("r1", {"c1": [["A"]], "c2": [["B"]]}),
    ])
    fmt = json.dumps({
        "table_block_column_order": ["c2", "c1"],  # 역순
        "table_block_column_header": True,
    })
    result = _reader()._render_table(conn, ["h", "r1"], fmt)
    expected = (
        "| 처리 | 상황 |\n"
        "| --- | --- |\n"
        "| B | A |"
    )
    assert result == expected, f"got:\n{result}"
    print("[PASS] TR-02 - 컬럼 순서 반영")


def test_tr03_no_header():
    """TR-03: 헤더 플래그 없으면 빈 헤더 행 생성"""
    conn = _make_conn([
        ("r1", {"c1": [["a"]], "c2": [["b"]]}),
    ])
    fmt = json.dumps({"table_block_column_order": ["c1", "c2"]})
    result = _reader()._render_table(conn, ["r1"], fmt)
    expected = (
        "|  |  |\n"
        "| --- | --- |\n"
        "| a | b |"
    )
    assert result == expected, f"got:\n{result}"
    print("[PASS] TR-03 - 헤더 없음")


def test_tr04_pipe_escaped():
    """TR-04: 셀 내 파이프(|)를 이스케이프한다"""
    conn = _make_conn([
        ("h", {"c1": [["H1"]], "c2": [["H2"]]}),
        ("r1", {"c1": [["a|b"]], "c2": [["c"]]}),
    ])
    fmt = json.dumps({
        "table_block_column_order": ["c1", "c2"],
        "table_block_column_header": True,
    })
    result = _reader()._render_table(conn, ["h", "r1"], fmt)
    assert "| a\\|b | c |" in result, f"got:\n{result}"
    print("[PASS] TR-04 - 파이프 이스케이프")


def test_tr05_ragged_rows_padded():
    """TR-05: 컬럼 순서 미지정 + 행별 키 수 불일치 시 패딩"""
    conn = _make_conn([
        ("r1", {"a": [["1"]], "b": [["2"]]}),
        ("r2", {"a": [["3"]]}),  # b 없음
    ])
    fmt = json.dumps({})  # column_order 없음, header 없음
    result = _reader()._render_table(conn, ["r1", "r2"], fmt)
    lines = result.split("\n")
    # 헤더(빈)+구분선+2행 = 4줄, 전부 2열
    assert lines[-1] == "| 3 |  |", f"got last line: {lines[-1]}"
    assert all(L.count("|") == 3 for L in lines), f"got:\n{result}"
    print("[PASS] TR-05 - 불규칙 행 패딩")


def test_tr06_empty_rows_fallback():
    """TR-06: 행이 없으면 [Table] 폴백"""
    conn = _make_conn([])
    result = _reader()._render_table(conn, [], json.dumps({}))
    assert result == "[Table]", f"got: {result}"
    print("[PASS] TR-06 - 빈 표 폴백")


def run_all_tests():
    """Run all table render tests"""
    print("\n" + "=" * 50)
    print("Table Render 단위 테스트")
    print("=" * 50 + "\n")

    tests = [
        test_tr01_header_table,
        test_tr02_column_order_respected,
        test_tr03_no_header,
        test_tr04_pipe_escaped,
        test_tr05_ragged_rows_padded,
        test_tr06_empty_rows_fallback,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"[FAIL] {test.__doc__}: {e}")
            failed += 1
        except Exception as e:
            print(f"[ERROR] {test.__doc__}: {e}")
            failed += 1

    print(f"\n결과: {passed}/{len(tests)} 통과")
    return passed, failed


if __name__ == "__main__":
    run_all_tests()
