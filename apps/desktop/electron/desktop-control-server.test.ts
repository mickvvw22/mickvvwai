import fs from 'node:fs'
import http from 'node:http'
import os from 'node:os'
import path from 'node:path'

import { afterEach, describe, expect, it, vi } from 'vitest'

import { createDesktopControlServer } from './desktop-control-server'

const tempDirs: string[] = []

function tempHome(): string {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'mickvvw-desktop-control-'))
  tempDirs.push(home)

  return home
}

function post(port: number, token?: string, route = '/v1/restart'): Promise<{ status: number; body: unknown }> {
  return new Promise((resolve, reject) => {
    const request = http.request({
      host: '127.0.0.1',
      port,
      method: 'POST',
      path: route,
      headers: token ? { Authorization: `Bearer ${token}` } : {}
    }, response => {
      const chunks: Buffer[] = []
      response.on('data', chunk => chunks.push(Buffer.from(chunk)))
      response.on('end', () => {
        resolve({ status: response.statusCode ?? 0, body: JSON.parse(Buffer.concat(chunks).toString('utf8')) })
      })
    })

    request.on('error', reject)
    request.end()
  })
}

afterEach(() => {
  for (const directory of tempDirs.splice(0)) {fs.rmSync(directory, { recursive: true, force: true })}
})

describe('Desktop control server', () => {
  it('binds only to loopback, writes a private descriptor, and authenticates restart requests', async () => {
    const onRestartRequested = vi.fn(() => ({ ok: true as const }))
    const onRestartAccepted = vi.fn()

    const control = createDesktopControlServer({
      hermesHome: tempHome(),
      makeToken: () => 'a'.repeat(64),
      onRestartRequested,
      onRestartAccepted
    })

    const descriptor = await control.start()

    try {
      expect(descriptor.host).toBe('127.0.0.1')
      expect(descriptor.port).toBeGreaterThan(0)
      expect(fs.statSync(control.descriptorPath).mode & 0o777).toBe(0o600)
      const reply = await post(descriptor.port, descriptor.token)
      expect(reply).toEqual({ status: 202, body: { ok: true, reason: 'restart_requested' } })
      expect(onRestartRequested).toHaveBeenCalledTimes(1)
      expect(onRestartAccepted).toHaveBeenCalledTimes(1)
    } finally {
      await control.close()
    }

    expect(fs.existsSync(control.descriptorPath)).toBe(false)
  })

  it('rejects missing credentials and routes without invoking the restart callback', async () => {
    const onRestartRequested = vi.fn(() => ({ ok: true as const }))

    const control = createDesktopControlServer({
      hermesHome: tempHome(),
      makeToken: () => 'b'.repeat(64),
      onRestartRequested
    })

    const descriptor = await control.start()

    try {
      expect((await post(descriptor.port)).status).toBe(401)
      expect((await post(descriptor.port, descriptor.token, '/other')).status).toBe(404)
      expect(onRestartRequested).not.toHaveBeenCalled()
    } finally {
      await control.close()
    }
  })

  it('refuses restart while work is active and leaves the control service available', async () => {
    const control = createDesktopControlServer({
      hermesHome: tempHome(),
      makeToken: () => 'c'.repeat(64),
      onRestartRequested: () => ({ ok: false, reason: 'active_work' })
    })

    const descriptor = await control.start()

    try {
      expect(await post(descriptor.port, descriptor.token)).toEqual({
        status: 409,
        body: { ok: false, reason: 'active_work' }
      })
      expect(fs.existsSync(control.descriptorPath)).toBe(true)
    } finally {
      await control.close()
    }
  })
})
