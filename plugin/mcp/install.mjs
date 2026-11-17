#!/usr/bin/env node
/**
 * Register the quant-desk kernel with WorkBuddy as a custom MCP connector.
 *
 *   node plugin/mcp/install.mjs              # install (idempotent)
 *   node plugin/mcp/install.mjs --uninstall
 *
 * It does three things:
 *   1. installs the MCP SDK if `plugin/mcp/node_modules` is missing
 *   2. merges the managed `quant-desk` fields in ~/.workbuddy/mcp.json,
 *      keeping other servers and unmanaged fields intact and backing up once
 *   3. links ~/.workbuddy/skills/quant-desk -> plugin/skills/quant-desk, so the
 *      operating procedure travels with the tools
 *
 * The host reads mcp.json at start-up, but a custom server still has to be
 * trusted once under 连接器 → 自定义连接器 before its tools appear.
 */

import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import process from 'node:process'
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const REPO = path.resolve(HERE, '..', '..')
const SERVER = path.join(HERE, 'server.mjs')
const SKILL_SRC = path.join(REPO, 'plugin', 'skills', 'quant-desk')
const HOME = os.homedir()
const WORKBUDDY_HOME = process.env.WORKBUDDY_HOME || path.join(HOME, '.workbuddy')
const MCP_CONFIG = path.join(WORKBUDDY_HOME, 'mcp.json')
const SKILLS_DIR = path.join(WORKBUDDY_HOME, 'skills')
const SKILL_LINK = path.join(SKILLS_DIR, 'quant-desk')
const SERVER_NAME = 'quant-desk'
const uninstall = process.argv.includes('--uninstall')

function fail(message) {
  console.error(`[quant-desk-mcp] ${message}`)
  process.exit(1)
}

function dotenvToken(text) {
  for (const row of text.split(/\r?\n/)) {
    const match = row.match(/^\s*(?:export\s+)?QUANT_TOKEN\s*=\s*(.*?)\s*$/)
    if (!match) continue
    const raw = match[1]
    const single = raw.match(/^'([^']*)'(?:\s+#.*)?$/)
    if (single) return single[1]
    const double = raw.match(/^"((?:\\.|[^"\\])*)"(?:\s+#.*)?$/)
    if (double) {
      try { return JSON.parse(`"${double[1]}"`) } catch { return double[1] }
    }
    return raw.replace(/\s+#.*$/, '').trim()
  }
  return undefined
}

// This file is copied out and run on its own (see the installer test, which
// executes it from a temp dir). It therefore cannot import `../lib/index.js`:
// the literal has to stay here even though it duplicates the shipped default.
// Rotating a token is an operator action on `.env`, not on this fallback.
function envToken(fallback = 'change-me-local-only') {
  try {
    const parsed = dotenvToken(fs.readFileSync(path.join(REPO, '.env'), 'utf8'))
    if (parsed !== undefined) return parsed
  } catch {
    // fall through to the existing entry or the documented default
  }
  return fallback
}

function writeJson(file, value) {
  const temp = `${file}.tmp-quant-${process.pid}-${Date.now()}`
  try {
    fs.writeFileSync(temp, `${JSON.stringify(value, null, 2)}\n`, { encoding: 'utf8', flag: 'wx' })
    fs.renameSync(temp, file)
  } finally {
    if (fs.existsSync(temp)) fs.unlinkSync(temp)
  }
}

function ensureDeps() {
  const entry = path.join(HERE, 'node_modules', '@modelcontextprotocol', 'sdk', 'package.json')
  if (fs.existsSync(entry)) return
  console.log('[quant-desk-mcp] installing @modelcontextprotocol/sdk ...')
  try {
    execFileSync(process.platform === 'win32' ? 'npm.cmd' : 'npm', ['install', '--no-audit', '--no-fund'], {
      cwd: HERE,
      stdio: 'inherit',
      shell: false,
    })
  } catch {
    fail('npm install failed; run it by hand inside plugin/mcp')
  }
}

function ensureSkillLink() {
  if (fs.existsSync(SKILL_LINK)) {
    const real = fs.realpathSync(SKILL_LINK)
    if (real === fs.realpathSync(SKILL_SRC)) return
    if (!uninstall) fail(`${SKILL_LINK} exists and points elsewhere (${real})`)
  }
  fs.mkdirSync(SKILLS_DIR, { recursive: true })
  fs.symlinkSync(SKILL_SRC, SKILL_LINK, 'junction')
  console.log(`[quant-desk-mcp] linked ${SKILL_LINK} -> ${SKILL_SRC}`)
}

function entry(existing = {}) {
  const oldEnv = existing.env && typeof existing.env === 'object' && !Array.isArray(existing.env)
    ? existing.env
    : {}
  return {
    ...existing,
    type: 'stdio',
    command: 'node',
    args: [SERVER],
    env: {
      ...oldEnv,
      QUANT_BASE_URL: process.env.QUANT_BASE_URL || oldEnv.QUANT_BASE_URL || 'http://127.0.0.1:8811',
      QUANT_TOKEN: process.env.QUANT_TOKEN || envToken(oldEnv.QUANT_TOKEN || 'change-me-local-only'),
      ...(process.env.QUANT_ALLOW_REMOTE ? { QUANT_ALLOW_REMOTE: process.env.QUANT_ALLOW_REMOTE } : {}),
    },
    description: 'A 股模拟盘交易台（quant-desk）：quant_* 工具，数字全部来自本机内核服务。',
  }
}

if (!uninstall) ensureDeps()

fs.mkdirSync(WORKBUDDY_HOME, { recursive: true })
let config = { mcpServers: {} }
if (fs.existsSync(MCP_CONFIG)) {
  const raw = fs.readFileSync(MCP_CONFIG, 'utf8')
  try {
    config = raw.trim() ? JSON.parse(raw) : config
  } catch {
    fail(`${MCP_CONFIG} is not valid JSON; fix it by hand first`)
  }
  const backup = `${MCP_CONFIG}.bak-quant`
  // Refresh it every run. "Only once" meant the restore point was the file as
  // it looked before the *first* install — useless after any later edit, and
  // silently older than the thing it claims to protect.
  try {
    fs.copyFileSync(MCP_CONFIG, backup)
  } catch (error) {
    fail(`cannot back up ${MCP_CONFIG}: ${error.message}`)
  }
}
config.mcpServers = config.mcpServers || {}

if (uninstall) {
  delete config.mcpServers[SERVER_NAME]
  writeJson(MCP_CONFIG, config)
  if (fs.existsSync(SKILL_LINK)) {
    fs.unlinkSync(SKILL_LINK)
    console.log(`[quant-desk-mcp] removed ${SKILL_LINK}`)
  }
  console.log('[quant-desk-mcp] uninstalled from WorkBuddy. Restart the app.')
  process.exit(0)
}

config.mcpServers[SERVER_NAME] = entry(config.mcpServers[SERVER_NAME])
writeJson(MCP_CONFIG, config)
ensureSkillLink()

console.log(`[quant-desk-mcp] wrote ${SERVER_NAME} -> ${MCP_CONFIG}`)
console.log('[quant-desk-mcp] quant_* tools; service must be up:')
console.log('  curl -s http://127.0.0.1:8811/health')
console.log('[quant-desk-mcp] next: WorkBuddy 连接器 → 自定义连接器 → 信任 quant-desk，然后重启')
