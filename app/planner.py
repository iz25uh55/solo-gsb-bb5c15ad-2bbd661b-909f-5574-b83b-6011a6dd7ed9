"""HTTP Range 回源规划核心逻辑（仅依赖标准库，便于独立测试）。

输入若干闭区间 [start, end] 或 suffix 范围（最后 N 个字节），先规范化为
对象内的闭区间，再按“允许的最大间隙”贪心合并为尽量少的回源块，同时保证
每个回源块长度不超过单次回源上限。

每个请求范围必须完整落在某一个回源块内（不会被拆到多个块），响应给出其
在该块中的切片偏移，使重叠范围可复用同一次回源得到的字节。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Span:
    """规范化后的闭区间。"""

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    def as_dict(self) -> dict:
        return {"start": self.start, "end": self.end}


class PlanError(ValueError):
    """输入越界或不可满足。index 为出错范围在请求中的下标，顶层错误为 None。"""

    def __init__(self, message: str, index: int | None = None):
        super().__init__(message)
        self.message = message
        self.index = index


@dataclass(frozen=True)
class Block:
    index: int
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    @property
    def fetch_header(self) -> str:
        """对应回源请求的 Range 头值。"""
        return f"bytes={self.start}-{self.end}"

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "start": self.start,
            "end": self.end,
            "length": self.length,
            "range_header": self.fetch_header,
        }


def _normalize_one(raw, total_length: int, max_fetch: int, index: int) -> Span:
    """把单个请求范围规范化为对象内闭区间，并做越界/可满足性校验。"""
    start = getattr(raw, "start", None)
    end = getattr(raw, "end", None)
    suffix = getattr(raw, "suffix", None)

    is_closed = start is not None or end is not None
    if is_closed and suffix is not None:
        raise PlanError("范围不能同时指定闭区间(start/end)与 suffix", index)
    if not is_closed and suffix is None:
        raise PlanError("范围必须指定闭区间(start 与 end)或 suffix", index)

    if is_closed:
        if start is None or end is None:
            raise PlanError("闭区间必须同时提供 start 与 end", index)
        if start < 0 or end < 0:
            raise PlanError("start 与 end 不能为负", index)
        if start > end:
            raise PlanError(f"闭区间 start({start}) 不能大于 end({end})", index)
        if end >= total_length:
            raise PlanError(
                f"end({end}) 越界，对象总长度为 {total_length}，"
                f"合法区间为 [0, {total_length - 1}]",
                index,
            )
        span = Span(start, end)
    else:
        if suffix <= 0:
            raise PlanError("suffix 必须 >= 1", index)
        if total_length == 0:
            raise PlanError("对象长度为 0，无法满足 suffix 范围", index)
        # HTTP suffix 语义：suffix 超过对象长度时回退为整个对象。
        start = max(0, total_length - suffix)
        span = Span(start, total_length - 1)

    if span.length > max_fetch:
        raise PlanError(
            f"范围长度 {span.length} 超过单次回源上限 {max_fetch}，无法满足",
            index,
        )
    return span


def build_plan(raw_ranges, total_length: int, max_gap: int, max_fetch: int) -> dict:
    """构建回源规划。

    返回 {"blocks": [...], "plans": [...]}，plans 与 raw_ranges 顺序一致。
    任何非法输入抛出 PlanError。
    """
    if total_length < 0:
        raise PlanError("total_length 必须 >= 0")
    if max_gap < 0:
        raise PlanError("max_gap 必须 >= 0")
    if max_fetch < 1:
        raise PlanError("max_fetch 必须 >= 1")

    spans = [
        _normalize_one(raw, total_length, max_fetch, i)
        for i, raw in enumerate(raw_ranges)
    ]

    # 去重后按起点排序：相同/重叠范围只参与一次分块，映射时复用同一块。
    # 贪心：只要与当前块的间隙 <= max_gap 且合并后长度 <= max_fetch，就并入；
    # 否则闭合当前块、另起一块。每个范围始终完整属于某一块，不会被拆开。
    blocks: list[Block] = []
    owner: dict[Span, Block] = {}
    group: list[Span] = []

    def flush_group(members: list[Span]):
        if not members:
            return
        block = Block(len(blocks), members[0].start, max(s.end for s in members))
        blocks.append(block)
        for s in members:
            owner[s] = block

    for span in sorted(set(spans), key=lambda s: (s.start, s.end)):
        if not group:
            group = [span]
            continue
        group_end = max(s.end for s in group)
        gap = span.start - group_end - 1  # 相邻或重叠时 gap <= 0
        merged_length = span.end - group[0].start + 1
        if gap <= max_gap and merged_length <= max_fetch:
            group.append(span)
        else:
            flush_group(group)
            group = [span]
    flush_group(group)

    plans = []
    for i, span in enumerate(spans):
        block = owner[span]
        plans.append(
            {
                "request_index": i,
                "block_index": block.index,
                "request_range": span.as_dict(),
                "length": span.length,
                "slice": {
                    # 在回源块字节缓冲中的闭切片，取出即可得到该范围的字节。
                    "start": span.start - block.start,
                    "end": span.end - block.start,
                    "length": span.length,
                },
            }
        )

    return {
        "blocks": [b.as_dict() for b in blocks],
        "plans": plans,
    }
