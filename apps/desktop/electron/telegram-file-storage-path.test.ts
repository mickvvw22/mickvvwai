import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import { resolveTelegramFileStorageRoot } from './telegram-file-storage-path'

function withTempRoot(run: (root: string) => void) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'telegram-file-storage-'))
  try {
    run(root)
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
}

test('packaged app prefers a writable product-specific directory beside the install', () => {
  withTempRoot(root => {
    const resourcesPath = path.join(root, 'Applications', 'Mickvvw', 'resources')
    fs.mkdirSync(resourcesPath, { recursive: true })

    const selected = resolveTelegramFileStorageRoot({
      packaged: true,
      platform: 'linux',
      resourcesPath,
      appUserDataPath: path.join(root, 'user-data'),
      appName: 'Mickvvw'
    })

    assert.equal(selected.location, 'install-sidecar')
    assert.equal(selected.root, path.join(root, 'Applications', 'Mickvvw-data', 'telegram_files'))
    assert.ok(fs.statSync(selected.root!).isDirectory())
  })
})

test('AppImage stores files beside the image, not in its mounted resources directory', () => {
  withTempRoot(root => {
    const appImagePath = path.join(root, 'portable', 'Mickvvw.AppImage')
    fs.mkdirSync(path.dirname(appImagePath), { recursive: true })

    const selected = resolveTelegramFileStorageRoot({
      packaged: true,
      platform: 'linux',
      resourcesPath: '/tmp/.mount-mickvvw/resources',
      appImagePath,
      appUserDataPath: path.join(root, 'user-data'),
      appName: 'Mickvvw'
    })

    assert.equal(selected.location, 'install-sidecar')
    assert.equal(selected.root, path.join(root, 'portable', 'Mickvvw-data', 'telegram_files'))
  })
})

test('read-only install locations fall back to app userData', () => {
  withTempRoot(root => {
    const userDataRoot = path.join(root, 'user-data', 'telegram_files')
    const selected = resolveTelegramFileStorageRoot({
      packaged: true,
      platform: 'linux',
      resourcesPath: path.join(root, 'read-only-app', 'resources'),
      appUserDataPath: path.join(root, 'user-data'),
      appName: 'Mickvvw',
      canWrite: directory => directory === userDataRoot
    })

    assert.equal(selected.location, 'user-data')
    assert.equal(selected.root, userDataRoot)
  })
})

test('development builds use app userData instead of the source tree', () => {
  withTempRoot(root => {
    const userDataRoot = path.join(root, 'user-data', 'telegram_files')
    const selected = resolveTelegramFileStorageRoot({
      packaged: false,
      platform: 'linux',
      resourcesPath: path.join(root, 'source', 'resources'),
      appUserDataPath: path.join(root, 'user-data'),
      appName: 'Mickvvw',
      canWrite: directory => directory === userDataRoot
    })

    assert.equal(selected.location, 'user-data')
    assert.equal(selected.root, userDataRoot)
  })
})
