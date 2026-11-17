#!/usr/bin/env node
/**
 * Install / uninstall the quant-desk DSH bundle.
 *
 *   node plugin/install.mjs            # install (idempotent, backs up package.json)
 *   node plugin/install.mjs --uninstall
 *
 * It does four things:
 *   1. links ~/.dsh/plugins/quant-desk -> this folder
 *   2. links profiles/desktop/node_modules/quant-desk -> this folder
 *   3. adds the dependency + bundle entry to the profile package.json
 *   4. links ~/.dsh/skills/quant-desk -> plugin/skills/quant-desk, so the desk
 *      is invocable as /quant-desk in every session, not only where the tools
 *      happen to be loaded
 *
 * Restart DSH afterwards; the plugin has no effect until the host reloads.
 * The skill link needs no restart: the skill scanner watches ~/.dsh/skills.
 */

import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import process from 'node:process'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const HOME = os.homedir()
const DSH_HOME = process.env.DSH_HOME || path.join(HOME, '.dsh')
const PROFILE = path.join(DSH_HOME, 'profiles', 'desktop')
const PROFILE_PKG = path.join(PROFILE, 'package.json')
const PLUGIN_LINK = path.join(DSH_HOME, 'plugins', 'quant-desk')
const MODULE_LINK = path.join(PROFILE, 'node_modules', 'quant-desk')
// User-level skill (rank 400): makes the desk invocable as /quant-desk in any
// session. The scanner follows symlinks, so the source stays in this repo.
const SKILL_SRC = path.join(HERE, 'skills', 'quant-desk')
const SKILL_LINK = path.join(DSH_HOME, 'skills', 'quant-desk')
const uninstall = process.argv.includes('--uninstall')
const DEFAULT_SDK_VERSION = '0.2.1-rc.3'
const SUPPORTED_SDK_VERSIONS = new Set(['0.2.1-rc.2', DEFAULT_SDK_VERSION])

function fail(message) {
  console.error(`[quant-desk] ${message}`)
  process.exit(1)
}

function ensureJunction(target, link) {
  if (fs.existsSync(link)) {
    const stat = fs.lstatSync(link)
    if (stat.isSymbolicLink() || stat.isDirectory()) {
      const real = fs.realpathSync(link)
      if (real === fs.realpathSync(target)) return
      if (!uninstall) fail(`${link} exists and points elsewhere (${real})`)
    }
  }
  fs.mkdirSync(path.dirname(link), { recursive: true })
  fs.symlinkSync(target, link, 'junction')
  console.log(`[quant-desk] linked ${link} -> ${target}`)
}

function removeLink(link) {
  if (!fs.existsSync(link)) return
  fs.unlinkSync(link)
  console.log(`[quant-desk] removed ${link}`)
}

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'))
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

if (!fs.existsSync(PROFILE_PKG)) fail(`profile not found: ${PROFILE_PKG}`)

const pkg = readJson(PROFILE_PKG)

if (uninstall) {
  removeLink(SKILL_LINK)
  removeLink(MODULE_LINK)
  removeLink(PLUGIN_LINK)
  if (pkg.dependencies) delete pkg.dependencies['quant-desk']
  if (pkg.dsh?.profile?.bundles) {
    pkg.dsh.profile.bundles = pkg.dsh.profile.bundles.filter((b) => b !== 'quant-desk')
  }
  writeJson(PROFILE_PKG, pkg)
  console.log('[quant-desk] uninstalled. Restart DSH to unload.')
  process.exit(0)
}

// 1) SDK: install the version that matches the host from npm.
//    rc.3 moved the app into app.asar, so linking to resources/app no longer
//    works; a pinned npm copy survives host updates.
function hostVersion() {
  const candidates = [
    process.env.DSH_APP_DIR,
    'D:\\Deepseek harness\\resources',
    path.join(path.dirname(process.execPath), 'resources'),
  ].filter(Boolean)
  for (const base of candidates) {
    const file = path.join(base, 'runtime', 'runtime.json')
    try {
      const json = JSON.parse(fs.readFileSync(file, 'utf8'))
      if (SUPPORTED_SDK_VERSIONS.has(json.desktopVersion)) return json.desktopVersion
      if (json.desktopVersion) {
        console.warn(`[quant-desk] unsupported desktopVersion ${JSON.stringify(json.desktopVersion)}; using ${DEFAULT_SDK_VERSION}`)
      }
    } catch {
      // try next
    }
  }
  return null
}

const version = hostVersion()
const pkgFile = path.join(HERE, 'package.json')
const pluginPkg = readJson(pkgFile)
const wanted = `@deepseek-ai/dsh-tools@${version || DEFAULT_SDK_VERSION}`
pluginPkg.dependencies = pluginPkg.dependencies || {}
pluginPkg.dependencies['@deepseek-ai/dsh-tools'] = version || DEFAULT_SDK_VERSION
writeJson(pkgFile, pluginPkg)

const sdkEntry = path.join(HERE, 'node_modules', '@deepseek-ai', 'dsh-tools', 'lib', 'index.js')
let needInstall = !fs.existsSync(sdkEntry)
if (!needInstall && version) {
  try {
    const installed = readJson(path.join(HERE, 'node_modules', '@deepseek-ai', 'dsh-tools', 'package.json'))
    needInstall = installed.version !== version
  } catch {
    needInstall = true
  }
}
if (needInstall) {
  const { execFileSync } = await import('node:child_process')
  console.log(`[quant-desk] installing ${wanted} ...`)
  try {
    execFileSync(process.platform === 'win32' ? 'npm.cmd' : 'npm', [
      'install', '--no-save', '--no-audit', '--no-fund', wanted,
    ], {
      cwd: HERE, stdio: 'inherit', shell: false,
    })
  } catch {
    console.warn('[quant-desk] npm install failed; the plugin will fall back to pass-through defineTool')
  }
}

// 2) plugin + module links
ensureJunction(HERE, PLUGIN_LINK)
ensureJunction(HERE, MODULE_LINK)
ensureJunction(SKILL_SRC, SKILL_LINK)

// 3) profile manifest
const backup = `${PROFILE_PKG}.bak-quant`
if (!fs.existsSync(backup)) fs.copyFileSync(PROFILE_PKG, backup)

pkg.dependencies = pkg.dependencies || {}
pkg.dependencies['quant-desk'] = 'file:../../plugins/quant-desk'
pkg.dsh = pkg.dsh || {}
pkg.dsh.profile = pkg.dsh.profile || {}
const bundles = new Set(pkg.dsh.profile.bundles || [])
bundles.add('quant-desk')
pkg.dsh.profile.bundles = [...bundles]
writeJson(PROFILE_PKG, pkg)

console.log('[quant-desk] installed.')
console.log(`[quant-desk] backup: ${backup}`)
console.log('[quant-desk] next: start the service, then restart DSH')
console.log('  .venv/Scripts/python.exe -m uvicorn service.api.app:app --host 127.0.0.1 --port 8811')
