import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.planner import PlanError, build_plan  # noqa: E402


@dataclass
class R:
    start: int | None = None
    end: int | None = None
    suffix: int | None = None


def closed(a, b):
    return R(start=a, end=b)


def suf(n):
    return R(suffix=n)


def block_headers(plan):
    return [b["range_header"] for b in plan["blocks"]]


def test_simple_disjoint_within_gap_merges():
    # [0,99] 与 [100,199] 相邻（gap=0），合并为一次回源
    plan = build_plan([closed(0, 99), closed(100, 199)], 1000, max_gap=16, max_fetch=10000)
    assert block_headers(plan) == ["bytes=0-199"]
    assert plan["plans"][0]["block_index"] == 0
    assert plan["plans"][0]["slice"] == {"start": 0, "end": 99, "length": 100}
    assert plan["plans"][1]["slice"] == {"start": 100, "end": 199, "length": 100}


def test_gap_boundary_is_inclusive():
    # gap = 50：允许 max_gap=50 合并，max_gap=49 不合并
    rs = [closed(0, 99), closed(150, 199)]
    assert len(build_plan(rs, 1000, 50, 10000)["blocks"]) == 1
    assert len(build_plan(rs, 1000, 49, 10000)["blocks"]) == 2


def test_overlapping_ranges_reuse_one_block():
    # 两个重叠范围 -> 同一块，字节可复用
    rs = [closed(10, 90), closed(50, 120)]
    plan = build_plan(rs, 1000, max_gap=0, max_fetch=10000)
    assert block_headers(plan) == ["bytes=10-120"]
    assert [p["block_index"] for p in plan["plans"]] == [0, 0]
    assert plan["plans"][1]["slice"] == {"start": 40, "end": 110, "length": 71}


def test_fetch_limit_forces_split_even_when_gap_allows():
    rs = [closed(0, 99), closed(200, 299)]  # gap=100，合并后 300 字节
    plan = build_plan(rs, 1000, max_gap=100, max_fetch=250)
    assert block_headers(plan) == ["bytes=0-99", "bytes=200-299"]
    assert all(b["length"] <= 250 for b in plan["blocks"])


def test_overlapping_range_too_big_for_one_block_splits_with_duplicate_bytes():
    # 三个范围：前两个合并恰好 <= 上限；第三个与第二重叠，但合进来会超限 -> 拆块
    rs = [closed(0, 99), closed(80, 199), closed(180, 299)]
    plan = build_plan(rs, 1000, max_gap=1000, max_fetch=220)
    assert block_headers(plan) == ["bytes=0-199", "bytes=180-299"]
    assert [p["block_index"] for p in plan["plans"]] == [0, 0, 1]
    assert plan["plans"][2]["slice"] == {"start": 0, "end": 119, "length": 120}


def test_response_order_matches_request_order():
    rs = [closed(900, 999), closed(0, 99), closed(400, 499)]
    plan = build_plan(rs, 1000, max_gap=0, max_fetch=10000)
    assert [p["request_index"] for p in plan["plans"]] == [0, 1, 2]
    assert plan["plans"][0]["request_range"] == {"start": 900, "end": 999}
    assert block_headers(plan) == ["bytes=0-99", "bytes=400-499", "bytes=900-999"]


def test_identical_ranges_map_to_same_block():
    rs = [closed(0, 99), closed(0, 99)]
    plan = build_plan(rs, 1000, max_gap=0, max_fetch=10000)
    assert len(plan["blocks"]) == 1
    assert [p["block_index"] for p in plan["plans"]] == [0, 0]


def test_suffix_normalization():
    plan = build_plan([suf(300)], 1000, max_gap=0, max_fetch=10000)
    assert plan["plans"][0]["request_range"] == {"start": 700, "end": 999}
    assert plan["plans"][0]["slice"] == {"start": 0, "end": 299, "length": 300}


def test_suffix_larger_than_object_becomes_whole_object():
    plan = build_plan([suf(5000)], 1000, max_gap=0, max_fetch=10000)
    assert plan["plans"][0]["request_range"] == {"start": 0, "end": 999}


def test_closed_end_at_last_byte_is_valid():
    plan = build_plan([closed(999, 999)], 1000, max_gap=0, max_fetch=10000)
    assert block_headers(plan) == ["bytes=999-999"]


def test_out_of_bound_closed_range_rejected():
    try:
        build_plan([closed(0, 1000)], 1000, 0, 10000)
        assert False, "应抛出 PlanError"
    except PlanError as e:
        assert e.index == 0


def test_start_after_end_rejected():
    try:
        build_plan([closed(50, 40)], 1000, 0, 10000)
        assert False
    except PlanError as e:
        assert e.index == 0


def test_single_range_exceeding_fetch_limit_rejected():
    try:
        build_plan([closed(0, 999)], 1000, 0, 500)
        assert False
    except PlanError as e:
        assert e.index == 0 and "上限" in e.message


def test_empty_object_suffix_rejected():
    try:
        build_plan([suf(1)], 0, 0, 10000)
        assert False
    except PlanError as e:
        assert e.index == 0


def test_empty_ranges_yields_empty_plan():
    plan = build_plan([], 1000, max_gap=0, max_fetch=10000)
    assert plan == {"blocks": [], "plans": []}


def test_bad_top_level_params():
    for kwargs in (
        dict(total_length=-1, max_gap=0, max_fetch=1),
        dict(total_length=1, max_gap=-1, max_fetch=1),
        dict(total_length=1, max_gap=0, max_fetch=0),
    ):
        try:
            build_plan([], **kwargs)
            assert False, kwargs
        except PlanError:
            pass


def test_block_lengths_never_exceed_limit_and_slices_reassemble():
    rs = [closed(0, 50), closed(1000, 1200), closed(1100, 1500), suf(100), closed(3000, 3099)]
    total, max_gap, max_fetch = 10_000, 64, 512
    plan = build_plan(rs, total, max_gap, max_fetch)

    for b in plan["blocks"]:
        assert b["length"] <= max_fetch
        assert b["start"] >= 0 and b["end"] < total

    # 切片映射无歧义：slice 相对块偏移换算回对象坐标后必须等于原规范化范围
    for p, raw in zip(plan["plans"], rs):
        b = plan["blocks"][p["block_index"]]
        s, e = p["slice"]["start"], p["slice"]["end"]
        assert (b["start"] + s, b["start"] + e) == (
            p["request_range"]["start"],
            p["request_range"]["end"],
        )
        assert s >= 0 and e < b["length"]
