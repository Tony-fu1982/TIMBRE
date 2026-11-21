"""DB 接缝：日志 seq 分页/去重、设置 upsert、JSON 往返（issue 04/08/09）。"""
from __future__ import annotations


def test_log_paging_and_dedupe(db):
    j = db.insert_job("download", "u", {})
    seqs = [db.append_log(j["id"], f"line{i}") for i in range(5)]
    assert seqs == [1, 2, 3, 4, 5]
    page1 = db.logs_after(j["id"], 0, 2)
    assert [l["seq"] for l in page1["lines"]] == [1, 2] and page1["has_more"]
    page2 = db.logs_after(j["id"], 2, 2)
    assert [l["seq"] for l in page2["lines"]] == [3, 4] and page2["has_more"]
    page3 = db.logs_after(j["id"], 4, 2)
    assert [l["seq"] for l in page3["lines"]] == [5] and not page3["has_more"]
    # SSE 重连去重：after_seq=已见最大 seq → 只拿新增
    db.append_log(j["id"], "line5")
    assert [l["seq"] for l in db.logs_after(j["id"], 5)["lines"]] == [6]
    assert db.snapshot(j["id"])["log_seq"] == 6


def test_settings_seed_and_upsert(db):
    s = db.settings()
    assert s["download_dir"] and s["debug_logs"] == "0"
    db.set_setting("download_dir", "D:\\out")
    db.set_setting("download_dir", "E:\\out2")
    assert db.settings()["download_dir"] == "E:\\out2"


def test_job_json_roundtrip(db):
    j = db.insert_job("generate", "主题", {"mode": "full", "dna": {"bpm": 120}})
    got = db.get_job(j["id"])
    assert got["options"]["dna"]["bpm"] == 120
    db.update_job(j["id"], result={"dnaProfile": {"style": "pop"}})
    assert db.get_job(j["id"])["result"]["dnaProfile"]["style"] == "pop"


def test_session_state(db):
    assert db.session()["state"] == "not_logged_in"
    db.set_session("logged_in", "a@b.c")
    assert db.session()["account"] == "a@b.c"