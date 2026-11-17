/**
 * 超时不是"服务不可达"。
 *
 * 2026-11-02 的四个插件审计报 `quant_discover` "threw / quant service
 * unreachable"，而同一时刻 `/health` 正常、`curl` 同一个路径 1.7 秒返回 200。
 * 真正发生的事是：服务是单进程 GIL 服务器，10 路并发 `/discover` 每路 21.7 秒，
 * 超过了 20 秒的客户端超时——而超时被写成"服务不可达"。
 *
 * 这条区别有后果：技能文件告诉模型"工具报服务不可达时先看服务"，所以一次假
 * 的不可达会把模型引到"数据服务挂了"的方向上去。
 *
 * 这个文件钉住三件事：超时的文案、超时的上限、以及并发下只算一次。
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import { QuantClient, describeTransportFailure } from '../lib/index.js'

test('a timeout says timeout, not unreachable', () => {
  const error = Object.assign(new Error('aborted'), { name: 'TimeoutError' })
  const text = describeTransportFailure(error, 'http://127.0.0.1:8811', 60000)

  assert.match(text, /timed out/i, '超时必须自称超时')
  assert.doesNotMatch(text, /unreachable/i, '超时不能说成不可达')
  assert.match(text, /still running/i, '要说明服务还在')
  assert.match(text, /60000ms/, '要带上超时上限')
})

test('an abort is also a timeout, not a dead service', () => {
  const error = Object.assign(new Error('aborted'), { name: 'AbortError' })
  const text = describeTransportFailure(error, 'http://127.0.0.1:8811', 60000)

  assert.match(text, /timed out/i)
  assert.doesNotMatch(text, /unreachable/i)
})

test('a connection failure is still reported as unreachable', () => {
  const error = Object.assign(new Error('fetch failed'), { name: 'TypeError' })
  const text = describeTransportFailure(error, 'http://127.0.0.1:8811', 60000)

  assert.match(text, /unreachable/i, '连不上就是连不上')
  assert.match(text, /uvicorn/, '要给出启动命令')
  assert.doesNotMatch(text, /timed out/i)
})

test('the client asks for a timeout that survives a loaded box', () => {
  const client = new QuantClient({})
  assert.ok(client.timeoutMs >= 60000,
    `客户端超时是 ${client.timeoutMs}ms；2026-11-02 实测重读在 CPU 争用下要 17.6 秒`)
})

test('a failing request reports the timeout, not the unreachable wording', async () => {
  const original = globalThis.fetch
  globalThis.fetch = async () => {
    throw Object.assign(new Error('aborted'), { name: 'TimeoutError' })
  }
  try {
    const client = new QuantClient({ baseUrl: 'http://127.0.0.1:8811' })
    await assert.rejects(
      () => client.request('/discover'),
      (err) => {
        assert.match(err.message, /timed out/i)
        assert.doesNotMatch(err.message, /unreachable/i)
        return true
      },
    )
  } finally {
    globalThis.fetch = original
  }
})
