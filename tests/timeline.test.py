# -*- coding: utf-8 -*-
"""提交时间线的后端行为测试：用 Flask 测试客户端驱动真实 SQLite。

覆盖：按实际提交顺序从 1 编号、栏位 / 首次成功加法前的值 / 提交后的值 /
按执行顺序排列的成功增量、失败加法不计入（写锁冲突 / 溢出 / 只读）、
回滚与纯读事务不记录、未提交内容不可见、提交重试成功只记一次、
大整数无损编码、/status 携带时间线且不改变最近显式读值、
删除准入（410）与未知实验（404）。

运行：python3 tests/timeline.test.py
"""

import os
import sys
import tempfile

# 独立数据目录，避免触碰仓库 data/；必须在 import app 之前设置
os.environ["LAB_DATA_DIR"] = tempfile.mkdtemp(prefix="lab-timeline-test-")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import app as lab  # noqa: E402

passed = 0
failed = 0


def check(cond, message):
    if not cond:
        raise AssertionError(message)


def test(name):
    def deco(fn):
        global passed, failed
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - 测试脚本直接报告
            failed += 1
            print("  ✗ %s\n      %s" % (name, exc))
        else:
            passed += 1
            print("  ✓ " + name)

    return deco


client = lab.app.test_client()


def create():
    resp = client.post("/api/experiments")
    check(resp.status_code == 200, "新建实验应返回 200，实际 %s" % resp.status_code)
    return resp.get_json()["id"]


def op(exp, side, operation, value=None):
    body = {"op": operation}
    if value is not None:
        body["value"] = value
    resp = client.post("/api/experiments/%s/conn/%s/op" % (exp, side), json=body)
    check(resp.status_code == 200, "操作应返回 200，实际 %s" % resp.status_code)
    return resp.get_json()


def set_readonly(exp, side, enabled):
    resp = client.post(
        "/api/experiments/%s/conn/%s/readonly" % (exp, side),
        json={"enabled": enabled},
    )
    check(resp.status_code == 200, "只读开关应返回 200")
    return resp.get_json()


def timeline(exp):
    resp = client.get("/api/experiments/%s/timeline" % exp)
    check(resp.status_code == 200, "时间线查询应返回 200，实际 %s" % resp.status_code)
    return resp.get_json()["timeline"]


@test("任务示例：甲 +5 +7 提交记 100→112 [5,7]；乙写锁失败后在原事务 +3 提交记 112→115 [3]")
def _():
    exp = create()
    check(timeline(exp) == [], "新实验时间线应为空")

    check(op(exp, "a", "begin")["ok"], "甲 BEGIN 应成功")
    check(op(exp, "a", "add", 5)["ok"], "甲 +5 应成功")
    check(op(exp, "a", "add", 7)["ok"], "甲 +7 应成功")
    check(timeline(exp) == [], "未提交内容不可见")

    check(op(exp, "b", "begin")["ok"], "乙 BEGIN 应成功")
    res = op(exp, "b", "add", 3)
    check(not res["ok"] and res["error"]["name"] == "SQLITE_BUSY", "乙应写锁冲突")
    check(timeline(exp) == [], "失败加法不产生记录")

    check(op(exp, "a", "commit")["ok"], "甲 COMMIT 应成功")
    check(
        timeline(exp)
        == [{"seq": 1, "side": "a", "before": 100, "after": 112, "deltas": [5, 7]}],
        "甲的记录应为 100→112 [5,7]，实际 %s" % timeline(exp),
    )

    # 乙在原事务重试 +3（无读快照，看到甲提交后的 112）
    check(op(exp, "b", "add", 3)["ok"], "乙原事务重试 +3 应成功")
    check(op(exp, "b", "commit")["ok"], "乙 COMMIT 应成功")
    check(
        timeline(exp)
        == [
            {"seq": 1, "side": "a", "before": 100, "after": 112, "deltas": [5, 7]},
            {"seq": 2, "side": "b", "before": 112, "after": 115, "deltas": [3]},
        ],
        "两条记录应按实际提交顺序编号，实际 %s" % timeline(exp),
    )


@test("失败加法不计入：溢出拒绝与只读拒绝的增量不出现在记录中")
def _():
    exp = create()
    big = 2 ** 62  # 单次增量上限
    op(exp, "a", "begin")
    check(op(exp, "a", "add", big)["ok"], "第一次加大数应成功")
    res = op(exp, "a", "add", big)
    check(not res["ok"] and "超出" in res["error"]["message"], "第二次应溢出拒绝")
    check(op(exp, "a", "add", -5)["ok"], "溢出拒绝后应可继续加法")

    check(set_readonly(exp, "a", True)["ok"], "开启只读应成功")
    res = op(exp, "a", "add", 8)
    check(
        not res["ok"] and res["error"]["name"] == "SQLITE_READONLY",
        "只读下加法应被 SQLite 拒绝",
    )
    check(set_readonly(exp, "a", False)["ok"], "关闭只读应成功")
    check(op(exp, "a", "add", 2)["ok"], "关闭只读后应可继续加法")
    check(op(exp, "a", "commit")["ok"], "COMMIT 应成功")

    # after 与第一个增量超出 ±2^53-1，按无损约定以十进制字符串传输
    check(
        timeline(exp)
        == [
            {
                "seq": 1,
                "side": "a",
                "before": 100,
                "after": str(100 + big - 5 + 2),
                "deltas": [str(big), -5, 2],
            }
        ],
        "只有成功增量按执行顺序入录，实际 %s" % timeline(exp),
    )


@test("回滚、纯读事务不产生记录；回滚后重开事务正常入录")
def _():
    exp = create()
    op(exp, "a", "begin")
    op(exp, "a", "add", 9)
    check(op(exp, "a", "rollback")["ok"], "ROLLBACK 应成功")
    check(timeline(exp) == [], "回滚事务不产生记录")

    op(exp, "a", "begin")
    check(op(exp, "a", "read")["ok"], "读值应成功")
    check(op(exp, "a", "commit")["ok"], "纯读事务 COMMIT 应成功")
    check(timeline(exp) == [], "纯读事务不产生记录")

    op(exp, "a", "begin")
    op(exp, "a", "add", 1)
    op(exp, "a", "commit")
    check(
        timeline(exp)
        == [{"seq": 1, "side": "a", "before": 100, "after": 101, "deltas": [1]}],
        "回滚后重开事务的记录应从 1 编号，实际 %s" % timeline(exp),
    )


@test("提交重试成功只记一次：首次 COMMIT 失败不产生记录，重试成功记一条")
def _():
    exp = create()
    op(exp, "a", "begin")
    op(exp, "a", "add", 4)

    # 包装连接注入一次 COMMIT 失败（事务保持开启，可重试）
    exp_obj = lab.experiments[exp]
    real_conn = exp_obj.conns["a"]

    class FlakyConn:
        def __init__(self, real):
            self._real = real
            self.failed = False

        @property
        def in_transaction(self):
            return self._real.in_transaction

        def execute(self, sql, parameters=()):
            if not self.failed and sql.strip().upper() == "COMMIT":
                self.failed = True
                raise lab.sqlite3.OperationalError("injected commit failure")
            return self._real.execute(sql, parameters)

    exp_obj.conns["a"] = FlakyConn(real_conn)
    try:
        res = op(exp, "a", "commit")
        check(not res["ok"], "注入的 COMMIT 应失败")
        check(timeline(exp) == [], "失败提交不产生记录")
        check(op(exp, "a", "commit")["ok"], "重试 COMMIT 应成功")
        check(
            timeline(exp)
            == [{"seq": 1, "side": "a", "before": 100, "after": 104, "deltas": [4]}],
            "提交重试成功只记一次，实际 %s" % timeline(exp),
        )
    finally:
        exp_obj.conns["a"] = real_conn


@test("大整数无损：超出安全整数范围的值以十进制字符串出现在记录中")
def _():
    exp = create()
    op(exp, "a", "begin")
    check(op(exp, "a", "add", "9007199254740993")["ok"], "字符串大整数加法应成功")
    op(exp, "a", "commit")
    check(
        timeline(exp)
        == [
            {
                "seq": 1,
                "side": "a",
                "before": 100,
                "after": "9007199254741093",
                "deltas": ["9007199254740993"],
            }
        ],
        "大整数应无损编码，实际 %s" % timeline(exp),
    )


@test("/status 携带时间线；采集与查询不改变最近显式读值")
def _():
    exp = create()
    op(exp, "a", "begin")
    check(op(exp, "a", "read")["ok"], "显式读值应成功")
    op(exp, "a", "add", 1)
    op(exp, "a", "commit")

    resp = client.get("/api/experiments/%s/status" % exp)
    check(resp.status_code == 200, "状态查询应返回 200")
    body = resp.get_json()
    check(
        body["timeline"]
        == [{"seq": 1, "side": "a", "before": 100, "after": 101, "deltas": [1]}],
        "/status 应携带时间线，实际 %s" % body.get("timeline"),
    )
    check(body["a"]["last_read"] == 100, "最近显式读值不应被时间线采集改变")

    # 事务内显式读值后的加法：首次成功加法前的值来自本事务快照
    op(exp, "a", "begin")
    op(exp, "a", "read")
    op(exp, "a", "add", 10)
    op(exp, "a", "add", 20)
    op(exp, "a", "commit")
    check(
        timeline(exp)[1]
        == {"seq": 2, "side": "a", "before": 101, "after": 131, "deltas": [10, 20]},
        "快照内事务的记录应为 101→131 [10,20]，实际 %s" % timeline(exp),
    )


@test("删除准入：删除后时间线查询 410，未知 ID 404；其它实验不受影响")
def _():
    exp = create()
    op(exp, "a", "begin")
    op(exp, "a", "add", 1)
    op(exp, "a", "commit")

    other = create()
    resp = client.delete("/api/experiments/%s" % exp)
    check(resp.status_code == 200, "删除应成功")

    resp = client.get("/api/experiments/%s/timeline" % exp)
    check(resp.status_code == 410, "删除后时间线应返回 410，实际 %s" % resp.status_code)
    resp = client.get("/api/experiments/%s/status" % exp)
    check(resp.status_code == 410, "删除后状态应返回 410")
    resp = client.get("/api/experiments/does-not-exist/timeline")
    check(resp.status_code == 404, "未知 ID 应返回 404，实际 %s" % resp.status_code)

    check(timeline(other) == [], "其它实验的时间线不受删除影响")


print("\n%d 通过，%d 失败" % (passed, failed))
sys.exit(0 if failed == 0 else 1)
