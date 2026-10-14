import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { test } from 'vitest'
import { prepareDmgbuild } from './prepare-dmgbuild.mjs'
import { ensureCommonJsToolsetScope, packagingTargetArch, pinnedPackageRoot } from './prepare-packaging-tools.mjs'

test('packager selects either same-OS payload architecture but refuses foreign OS', () => {
  for (const arch of ['x64', 'arm64']) assert.equal(packagingTargetArch(`${process.platform}-${arch}`), arch)
  const foreign = process.platform === 'darwin' ? 'win32' : 'darwin'
  assert.throws(() => packagingTargetArch(`${foreign}-arm64`), /same-OS/)
  assert.throws(() => packagingTargetArch(`${process.platform}-ia32`), /same-OS/)
})

test('packager accepts a lock-matched package in a pnpm virtual store and rejects a different version', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'packager-pnpm-'))
  try {
    const desktop = path.join(root, 'apps/desktop')
    const nodeModules = path.join(desktop, 'node_modules')
    const packageRoot = path.join(nodeModules, '.pnpm/electron-builder@1.2.3/node_modules/electron-builder')
    fs.mkdirSync(packageRoot, { recursive: true })
    fs.writeFileSync(path.join(desktop, 'package.json'), '{}')
    fs.writeFileSync(
      path.join(packageRoot, 'package.json'),
      JSON.stringify({ name: 'electron-builder', version: '1.2.3', main: 'index.js' })
    )
    fs.writeFileSync(path.join(packageRoot, 'index.js'), 'export {}')
    fs.symlinkSync(
      path.relative(nodeModules, packageRoot),
      path.join(nodeModules, 'electron-builder'),
      process.platform === 'win32' ? 'junction' : 'dir'
    )
    fs.writeFileSync(
      path.join(root, 'package-lock.json'),
      JSON.stringify({ packages: { 'node_modules/electron-builder': { version: '1.2.3' } } })
    )

    assert.equal(pinnedPackageRoot(root, 'electron-builder'), packageRoot)
    fs.writeFileSync(
      path.join(packageRoot, 'package.json'),
      JSON.stringify({ name: 'electron-builder', version: '1.2.4', main: 'index.js' })
    )
    assert.throws(() => pinnedPackageRoot(root, 'electron-builder'), /lock-pinned package/)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('icon toolset gets a CommonJS package scope independent of the desktop app', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'icon-tool-scope-'))
  try {
    fs.writeFileSync(path.join(root, 'icon-tool.js'), 'require("fs")')
    ensureCommonJsToolsetScope(root)
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8')), { type: 'commonjs' })
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})

test('explicit PM supplier is copied with paired Python; missing runtime fails before publication', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dmgbuild-prepare-'))
  try {
    const vendor = path.join(root, 'pm-tools/vendor')
    const binary = path.join(vendor, 'dmgbuild')
    const python = path.join(vendor, 'python/bin/python3')
    fs.mkdirSync(path.dirname(python), { recursive: true })
    fs.writeFileSync(binary, '#!/usr/bin/env bash\nexit 1\n')
    fs.writeFileSync(python, 'paired Python fixture')
    const out = path.join(root, 'packager')
    const copied = prepareDmgbuild({ source: root, out, cache: path.join(root, 'cache'), binary })
    assert.equal(copied, path.join(out, 'dmgbuild/dmgbuild'))
    assert.equal(fs.readFileSync(path.join(path.dirname(copied), 'python/bin/python3'), 'utf8'), 'paired Python fixture')
    fs.writeFileSync(path.join(path.dirname(copied), 'python/bin/python3'), 'build-local change')
    assert.equal(fs.readFileSync(python, 'utf8'), 'paired Python fixture')
    fs.rmSync(python)
    assert.throws(() => prepareDmgbuild({ source: root, out, cache: root, binary }), /paired Python/)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})
