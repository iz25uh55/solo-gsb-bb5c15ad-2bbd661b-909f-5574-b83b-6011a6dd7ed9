# Range 回源规划服务

为对象存储代理规划 HTTP Range 回源块：规范化闭区间/suffix 范围，校验越界与可满足性，
按最大间隙合并回源区间且不超过单次回源上限，并返回每个请求范围在回源块中的切片映射。

## 启动

```bash
docker compose up --build -d
```

## 必需配置

请求体 `POST http://localhost:8080/plan`（JSON）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `total_length` | int ≥ 0 | 对象总长度（字节） |
| `ranges` | array | 每项二选一：`{"start":0,"end":99}` 闭区间（含端点），或 `{"suffix":500}` 最后 N 字节 |
| `max_gap` | int ≥ 0 | 允许合并的最大间隙（字节），相邻区间间隙为 0；省略时取环境变量 `MAX_GAP_DEFAULT`（默认 65536） |
| `max_fetch` | int ≥ 1 | 单次回源长度上限（字节）；省略时取环境变量 `MAX_FETCH_DEFAULT`（默认 8388608） |

非法输入（端点越界、start>end、单范围超过 `max_fetch` 等）返回 `422` 并标出 `request_index`。

## 端口

服务监听 `http://localhost:8080`（`POST /plan`，`GET /healthz`）。
