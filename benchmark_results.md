# Graphionica 4.6.1 gateway benchmark

- 生成时间: 2026-10-29T14:53:10Z
- 网关: http://127.0.0.1:8960
- rounds: 3

## 单请求延迟

| 分组 | 场景 | median | min | max | 备注 |
|------|------|--------|-----|-----|------|
| cache-hit | gateway /health | 15.8ms | 6.0ms | 18.0ms |  |
| cache-hit | gateway /catalog/summary | 5.5ms | 4.0ms | 24.4ms |  |
| cache-hit | gateway /catalog/stickers (p1,60) | 5.5ms | 5.1ms | 26.8ms |  |
| cache-hit | gateway /catalog/templates (p1,24) | 5.3ms | 4.9ms | 5.3ms |  |
| cache-hit | gateway /api/v2/home (incremental) | 4.1ms | 4.1ms | 27.3ms |  |
| cache-hit | gateway /export/manifest.json | 138.7ms | 134.5ms | 145.9ms |  |
| asset-cold | gateway /asset/s/1_flowers/preview/1.webp | 476.2ms | 476.2ms | 476.2ms | 本地无缓存 → 回源 CDN |
| asset-warm | gateway /asset/s/1_flowers/preview/1.webp | 21.5ms | 4.1ms | 31.0ms | 本地磁盘命中 |
| upstream | upstream POST /home | 519.9ms | 509.4ms | 2083.2ms |  |
| upstream | upstream POST /stickers | 754.7ms | 725.1ms | 803.3ms |  |
| upstream | upstream POST /templates | 1161.7ms | 1126.9ms | 1360.4ms | 上游最慢端点 |
| upstream | upstream CDN sticker (203KB) | 1174.2ms | 828.8ms | 3217.6ms |  |

## 并发曲线 (`/catalog/stickers`, 120 请求)

| 并发 | RPS | p50 | p95 | 失败 |
|------|-----|-----|-----|------|
| 1 | 126.8 | 5.8ms | 25.9ms | 0 |
| 4 | 261.0 | 14.8ms | 20.6ms | 0 |
| 8 | 263.6 | 26.3ms | 31.4ms | 0 |
| 16 | 201.2 | 61.6ms | 195.1ms | 0 |
