import { EventEmitter } from 'node:events'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, describe, expect, it, vi } from 'vitest'

const spawnMock = vi.hoisted(() => vi.fn())

vi.mock('node:child_process', () => ({ spawn: spawnMock }))

import { spawn } from 'node:child_process'

import { createGuardianManager } from './guardian'

const mockedSpawn = vi.mocked(spawn)
const roots: string[] = []
const managers: ReturnType<typeof createGuardianManager>[] = []

function makeChild(
  pid: number
): EventEmitter & { pid: number; unref: ReturnType<typeof vi.fn>; kill: ReturnType<typeof vi.fn> } {
  const child = new EventEmitter() as EventEmitter & {
    pid: number
    unref: ReturnType<typeof vi.fn>
    kill: ReturnType<typeof vi.fn>
  }

  child.pid = pid
  child.unref = vi.fn()
  child.kill = vi.fn().mockReturnValue(true)

  return child
}

function makeManager() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'mickvvw-guardian-test-'))
  roots.push(root)
  const resourcesPath = path.join(root, 'resources')
  const payloadPath = path.join(resourcesPath, 'agent-payload')
  const userDataPath = path.join(root, 'user-data')
  const home = path.join(root, 'home')
  const configHome = path.join(root, 'config')

  const runtime = {
    storePython: 'tools/python/bin/python3',
    repoDir: 'hermes-agent',
    sitePackages: 'venv/lib/python3.14/site-packages',
    toolsDir: 'tools',
    commands: { hermes: 'bin/hermes' }
  }

  for (const directory of [
    path.join(payloadPath, 'tools/python/bin'),
    path.join(payloadPath, runtime.repoDir),
    path.join(payloadPath, runtime.sitePackages),
    path.join(payloadPath, runtime.toolsDir),
    path.join(payloadPath, 'bin'),
    resourcesPath,
    userDataPath,
    home
  ]) {
    fs.mkdirSync(directory, { recursive: true })
  }

  fs.writeFileSync(path.join(payloadPath, 'manifest.json'), JSON.stringify({ runtime }))
  fs.writeFileSync(path.join(payloadPath, runtime.storePython), '#!/bin/sh\n')
  fs.chmodSync(path.join(payloadPath, runtime.storePython), 0o755)
  fs.writeFileSync(path.join(resourcesPath, 'guardian-supervisor.py'), '# test helper\n')
  fs.writeFileSync(path.join(payloadPath, runtime.commands.hermes), '#!/bin/sh\n')
  fs.chmodSync(path.join(payloadPath, runtime.commands.hermes), 0o755)

  const manager = createGuardianManager({
    platform: 'linux',
    packaged: true,
    resourcesPath,
    userDataPath,
    home,
    hermesHome: path.join(home, '.hermes'),
    profile: null,
    appCommand: path.join(root, 'Mickvvw'),
    manageGateway: true,
    environment: {
      HOME: home,
      PATH: '/usr/bin:/bin',
      XDG_CONFIG_HOME: configHome,
      DISPLAY: ':99',
      LD_LIBRARY_PATH: path.join(root, 'usr', 'lib')
    }
  })

  managers.push(manager)

  return { manager, userDataPath }
}

afterEach(() => {
  for (const manager of managers.splice(0)) {
    manager.stopForUser()
  }

  for (const root of roots.splice(0)) {
    fs.rmSync(root, { recursive: true, force: true })
  }

  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('Guardian supervisor startup', () => {
  it('passes AppRun library paths to the helper and persists spawn error codes without error text', async () => {
    const child = makeChild(41001)
    mockedSpawn.mockReturnValueOnce(child as never)
    const { manager, userDataPath } = makeManager()

    const status = await manager.initialize()
    const config = JSON.parse(fs.readFileSync(path.join(userDataPath, 'guardian', 'config.json'), 'utf8'))
    const diagnosticsPath = path.join(userDataPath, 'guardian', 'diagnostics.jsonl')
    const spawnOptions = mockedSpawn.mock.calls[0]?.[2] as { env: Record<string, string> }

    expect(status.enabled).toBe(true)
    expect(config.version).toBe(2)
    expect(spawnOptions.env.LD_LIBRARY_PATH).toBe(config.environment.LD_LIBRARY_PATH)
    expect(config.environment.LD_LIBRARY_PATH).toMatch(/usr[\\/]lib$/)

    child.emit(
      'error',
      Object.assign(new Error('do not persist the full message'), {
        code: 'ENOENT',
        errno: -2,
        syscall: 'spawn'
      })
    )

    expect(manager.getStatus().lastError).toBe('supervisor_start_failed')

    const [diagnostic] = fs
      .readFileSync(diagnosticsPath, 'utf8')
      .trim()
      .split('\n')
      .map(line => JSON.parse(line))

    expect(diagnostic).toMatchObject({
      event: 'supervisor_spawn_error',
      code: 'ENOENT',
      errno: -2,
      syscall: 'spawn'
    })
    expect(diagnostic.message).toBeUndefined()
    expect(fs.statSync(diagnosticsPath).mode & 0o777).toBe(0o600)
  })

  it('records an early helper exit and retries with bounded backoff', async () => {
    vi.useFakeTimers()
    const firstChild = makeChild(41002)
    const nextChild = makeChild(41003)
    mockedSpawn.mockReturnValueOnce(firstChild as never).mockReturnValueOnce(nextChild as never)
    const { manager, userDataPath } = makeManager()
    const initialize = manager.initialize()

    await vi.advanceTimersByTimeAsync(250)
    await initialize
    firstChild.emit('spawn')
    firstChild.emit('exit', 127, null)

    expect(manager.getStatus().lastError).toBe('supervisor_exited_before_ready')
    await vi.advanceTimersByTimeAsync(2_000)
    expect(mockedSpawn).toHaveBeenCalledTimes(2)

    const diagnosticsPath = path.join(userDataPath, 'guardian', 'diagnostics.jsonl')

    const diagnostics = fs
      .readFileSync(diagnosticsPath, 'utf8')
      .trim()
      .split('\n')
      .map(line => JSON.parse(line))

    expect(diagnostics).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ event: 'supervisor_exit_before_ready', exitCode: 127 }),
        expect.objectContaining({ event: 'supervisor_retry_scheduled', attempt: 1, delayMs: 2_000 })
      ])
    )
  })

  it('does not restart the supervisor after an explicit user stop', async () => {
    vi.useFakeTimers()
    const child = makeChild(41004)
    mockedSpawn.mockReturnValueOnce(child as never)
    const { manager } = makeManager()
    const initialize = manager.initialize()

    await vi.advanceTimersByTimeAsync(250)
    await initialize
    child.emit('spawn')
    manager.stopForUser()
    child.emit('exit', 0, null)
    await vi.advanceTimersByTimeAsync(60_000)

    expect(mockedSpawn).toHaveBeenCalledTimes(1)
  })
})
