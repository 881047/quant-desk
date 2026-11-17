#!/usr/bin/env node
/**
 * 工具层的覆盖率闸。
 *
 * 为什么 Python 侧的 `scripts/coverage_gate.py` 盯不住这里：65 个 `quant_*` 工具
 * 全部住在 `lib/index.js`（108 KB），它们既不在 `service/` 下，也不在 pytest 的
 * 统计范围里。13:00 修过"四个零覆盖工具"，那是 `tape.py`——工具层从来没进过闸。
 *
 * 为什么阈值盯 line 而不是"工具数"：两者答的不是一个问题。
 *  - "每个工具至少被调用过一次"由 `test/tools.test.mjs` 里那条结构用例保证
 *    （按 schema 的必填项自动填参数，要求每条工具真的发出一次请求）。它是硬的。
 *  - 覆盖率管的是另一半：**一个工具内部的每个分支**——错误信封、裁剪梯队、
 *    空结果——有没有人走过。加工具时前者会红，改分支时后者会红。
 *
 * 2026-11-04 的基线（这条闸建立时）：index.js 95.92 / 84.08 / 96.15，
 * server.mjs 100 / 86.49 / 75。门槛留了余量，不是刚好压线。
 *
 * 不用 `c8`：`node --test-coverage-*` 从 v22 起自带阈值判定，退出码就是结论。
 * 少一个依赖，也少一层"它到底测了哪些文件"的解释。
 *
 *     node coverage_gate.mjs        # 跑两个套件 + 判阈值
 *     npm run test:coverage
 */

import { spawnSync } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))

/**
 * 门槛。三条都给，因为它们的失败方式不同：改描述会先掉 branch，
 * 漏测一个新分支会先掉 line，抽函数不测会先掉 functions。
 *
 * functions 的天花板是 100 减不掉的：`mcp/server.mjs` 的 `main()` 显式豁免了
 * （它由 e2e 子进程覆盖，v8 不跨进程收集），所以实际最高是 75 那条线上。
 */
const FLOORS = { lines: 95, branches: 80, functions: 72 }

const args = [
  '--experimental-test-coverage',
  `--test-coverage-lines=${FLOORS.lines}`,
  `--test-coverage-branches=${FLOORS.branches}`,
  `--test-coverage-functions=${FLOORS.functions}`,
  // 只统计产品代码。把测试自己也算进去，覆盖率会被测试文件本身抬高。
  '--test-coverage-include=lib/*.js',
  '--test-coverage-include=mcp/*.mjs',
  '--test',
  'test/*.test.mjs',
  'mcp/test/*.test.mjs',
]

const run = spawnSync(process.execPath, args, { cwd: HERE, stdio: 'inherit' })

if (run.error) {
  console.error(`[coverage_gate] 跑不起来：${run.error.message}`)
  process.exit(2)
}

if (run.status !== 0) {
  console.error(`[coverage_gate] 未通过（node 退出码 ${run.status}）：`
    + `line ≥ ${FLOORS.lines} / branch ≥ ${FLOORS.branches} / funcs ≥ ${FLOORS.functions}`)
  process.exit(1)
}

console.log(`[coverage_gate] 全部达标：line ≥ ${FLOORS.lines}、`
  + `branch ≥ ${FLOORS.branches}、funcs ≥ ${FLOORS.functions}`)
