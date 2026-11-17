# quant-desk · MCP adapter

把同一个量化内核接进任何说 MCP 的宿主（WorkBuddy、Claude Desktop、Cursor…）。
它不含任何业务逻辑，也不复制工具表：

```
MCP client ──stdio──> server.mjs ──┐
                                   ├─ buildTools()  ← ../lib/index.js（唯一来源）
DSH 宿主 ────cordis──> lib/index.js┘
                                   └─ HTTP ──> 127.0.0.1:8811（内核服务）
```

4 个公开 `quant_*` 工具、描述与 guard 全部从同一张表导入；73 个旧名只作为隐藏兼容别名保留。
插件里加一个内部 handler，MCP 这边**不需要第二次修改**。

## 安装（WorkBuddy）

```bash
cd D:/quant-desk
.venv/Scripts/python.exe -m uvicorn service.api.app:app --host 127.0.0.1 --port 8811   # 服务常驻
node plugin/mcp/install.mjs                                                            # 写配置 + 链接技能
```

然后：**WorkBuddy → 连接器 → 自定义连接器 → 信任 `quant-desk` → 重启**。
不点「信任」工具不会出现，这是最常见的卡点。

卸载：`node plugin/mcp/install.mjs --uninstall`（只删除 `quant-desk` 这一项与技能链接，
其它 MCP 服务不动；写入前会留一份 `~/.workbuddy/mcp.json.bak-quant`）。

## 配置

`~/.workbuddy/mcp.json` 里生成的那一段：

```json
{
  "mcpServers": {
    "quant-desk": {
      "type": "stdio",
      "command": "node",
      "args": ["D:\\quant-desk\\plugin\\mcp\\server.mjs"],
      "env": {
        "QUANT_BASE_URL": "http://127.0.0.1:8811",
        "QUANT_TOKEN": "change-me-local-only"
      }
    }
  }
}
```

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `QUANT_BASE_URL` | `http://127.0.0.1:8811` | 内核服务地址 |
| `QUANT_TOKEN` | `.env` 里的 `QUANT_TOKEN` | `X-Quant-Token` |
| `QUANT_TIMEOUT_MS` | `60000` | 单次调用超时；有限整数 1–300000。60 s 不是拍的：服务是单进程，重读在 CPU 竞争下实测 17.6 s，20 s 上限会把它误报成"不可达" |
| `QUANT_MAX_RESPONSE_BYTES` | `1048576` | 响应流字节硬上限，超限取消 reader |
| `QUANT_MAX_OUTPUT` | `16000` | 返回体上限，按 **UTF-8 字节**算（16,000 字节约 5,300 个汉字）。超了先按阶梯压缩并带 `_shrink`；全压不下才返回 `output too large` 信封 + `next` |
| `QUANT_ALLOW_REMOTE` | 未设置 | 非 loopback 服务必须显式设为 `1`；URL 禁止 userinfo，重定向一律拒绝 |

工具多的时候可以给这一项加 `"defer_loading": true`，让宿主按需加载；代价是
首次使用时多一次检索。

## 一次调用的路径

1. `guard()`（与 DSH 同一份）先拒绝非法参数：提案缺理由、夹带 `price`、
   `qty` 非正整数/超限或与 `target_weight` 同给、批准没写 `confirm=true`。
2. 工具跑在一个**只记录不发请求**的 client 上，得到它本该发出的
   `{path, method, query, body}`——所以这里没有第二份路由表。
3. 由真实的 `QuantClient` 发出请求，错误文本、超时、token 全部复用插件实现。

stdout 只承载 JSON-RPC 帧；诊断信息一律走 stderr。

## 测试

```bash
cd plugin/mcp && npm test    # 握手、动态工具表、参数透传、写路径、guard、安装器等
cd plugin && npm test        # DSH 契约、网络安全、输出与安装器测试
```

测试用桩 HTTP 服务，不依赖真实行情，也不需要服务在线。
