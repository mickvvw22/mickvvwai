import crypto from 'node:crypto'
import fs from 'node:fs'
import http, { type IncomingMessage, type Server, type ServerResponse } from 'node:http'
import path from 'node:path'

export const DESKTOP_CONTROL_DESCRIPTOR = 'desktop-control.json'
const DESKTOP_CONTROL_ROUTE = '/v1/restart'
const TOKEN_BYTES = 32

export interface DesktopRestartDecision {
  ok: boolean
  reason?: 'active_work' | 'busy' | 'restart_failed'
}

export interface DesktopControlDescriptor {
  version: 1
  host: '127.0.0.1'
  port: number
  pid: number
  token: string
}

export interface DesktopControlServerOptions {
  hermesHome: string
  onRestartRequested: () => Promise<DesktopRestartDecision> | DesktopRestartDecision
  onRestartAccepted?: () => void
  makeToken?: () => string
}

export interface DesktopControlServer {
  start(): Promise<DesktopControlDescriptor>
  close(): Promise<void>
  descriptorPath: string
}

function sendJson(response: ServerResponse, status: number, payload: Record<string, unknown>): void {
  response.writeHead(status, {
    'Cache-Control': 'no-store',
    Connection: 'close',
    'Content-Type': 'application/json; charset=utf-8',
    'X-Content-Type-Options': 'nosniff'
  })
  response.end(JSON.stringify(payload))
}

function isLoopbackAddress(address: string | undefined): boolean {
  if (!address) {return false}
  const normalized = address.toLowerCase().replace(/^::ffff:/, '')

  return normalized === '127.0.0.1' || normalized === '::1'
}

function authorizationMatches(request: IncomingMessage, token: string): boolean {
  const value = request.headers.authorization

  if (typeof value !== 'string') {return false}
  const actual = Buffer.from(value)
  const expected = Buffer.from(`Bearer ${token}`)

  return actual.length === expected.length && crypto.timingSafeEqual(actual, expected)
}

function atomicWriteDescriptor(filePath: string, descriptor: DesktopControlDescriptor): void {
  const tempPath = `${filePath}.${process.pid}.${crypto.randomBytes(8).toString('hex')}.tmp`

  try {
    fs.writeFileSync(tempPath, JSON.stringify(descriptor), { encoding: 'utf8', flag: 'wx', mode: 0o600 })
    fs.renameSync(tempPath, filePath)
  } catch (error) {
    try {
      fs.unlinkSync(tempPath)
    } catch {
      // The temp file may not have been created.
    }

    throw error
  }
}

export function createDesktopControlServer(options: DesktopControlServerOptions): DesktopControlServer {
  const descriptorPath = path.join(options.hermesHome, 'state', DESKTOP_CONTROL_DESCRIPTOR)
  let server: Server | null = null
  let descriptor: DesktopControlDescriptor | null = null
  let closing: Promise<void> | null = null

  const handler = async (request: IncomingMessage, response: ServerResponse): Promise<void> => {
    if (!isLoopbackAddress(request.socket.remoteAddress)) {
      request.resume()
      sendJson(response, 403, { ok: false, reason: 'unavailable' })

      return
    }

    if (request.method !== 'POST' || request.url !== DESKTOP_CONTROL_ROUTE) {
      request.resume()
      sendJson(response, 404, { ok: false, reason: 'not_found' })

      return
    }

    if (!descriptor || !authorizationMatches(request, descriptor.token)) {
      request.resume()
      sendJson(response, 401, { ok: false, reason: 'unavailable' })

      return
    }

    const contentLength = Number(request.headers['content-length'] ?? 0)

    if (Number.isFinite(contentLength) && contentLength > 0) {
      request.resume()
      sendJson(response, 400, { ok: false, reason: 'bad_request' })

      return
    }

    request.resume()

    try {
      const decision = await options.onRestartRequested()

      if (!decision.ok) {
        sendJson(response, decision.reason === 'active_work' || decision.reason === 'busy' ? 409 : 503, {
          ok: false,
          reason: decision.reason ?? 'restart_failed'
        })

        return
      }

      response.writeHead(202, {
        'Cache-Control': 'no-store',
        Connection: 'close',
        'Content-Type': 'application/json; charset=utf-8',
        'X-Content-Type-Options': 'nosniff'
      })
      response.end(JSON.stringify({ ok: true, reason: 'restart_requested' }), () => {
        try {
          options.onRestartAccepted?.()
        } catch {
          // The restart request is already acknowledged; process-level logging is owned by the caller.
        }
      })
    } catch {
      sendJson(response, 503, { ok: false, reason: 'restart_failed' })
    }
  }

  return {
    descriptorPath,
    async start(): Promise<DesktopControlDescriptor> {
      if (server && descriptor) {return descriptor}
      fs.mkdirSync(path.dirname(descriptorPath), { recursive: true, mode: 0o700 })
      const token = (options.makeToken ?? (() => crypto.randomBytes(TOKEN_BYTES).toString('hex')))()

      if (!/^[a-f0-9]{64}$/i.test(token)) {throw new Error('Desktop control token must be 32 random bytes in hex form')}

      const nextServer = http.createServer((request, response) => {
        void handler(request, response)
      })

      await new Promise<void>((resolve, reject) => {
        nextServer.once('error', reject)
        nextServer.listen(0, '127.0.0.1', () => {
          nextServer.removeListener('error', reject)
          resolve()
        })
      })
      const address = nextServer.address()

      if (!address || typeof address === 'string' || address.address !== '127.0.0.1') {
        await new Promise<void>(resolve => nextServer.close(() => resolve()))
        throw new Error('Desktop control server did not bind to IPv4 loopback')
      }

      const nextDescriptor: DesktopControlDescriptor = {
        version: 1,
        host: '127.0.0.1',
        port: address.port,
        pid: process.pid,
        token
      }

      try {
        atomicWriteDescriptor(descriptorPath, nextDescriptor)
      } catch (error) {
        await new Promise<void>(resolve => nextServer.close(() => resolve()))
        throw error
      }

      server = nextServer
      descriptor = nextDescriptor

      return nextDescriptor
    },
    async close(): Promise<void> {
      if (closing) {return closing}
      const ownedDescriptor = descriptor
      const ownedServer = server
      descriptor = null
      server = null
      closing = (async () => {
        if (ownedServer) {
          await new Promise<void>(resolve => ownedServer.close(() => resolve()))
        }

        if (ownedDescriptor) {
          try {
            const current = JSON.parse(fs.readFileSync(descriptorPath, 'utf8')) as Partial<DesktopControlDescriptor>

            if (current.token === ownedDescriptor.token) {fs.unlinkSync(descriptorPath)}
          } catch {
            // A stale descriptor is harmless and must not block application shutdown.
          }
        }
      })()

      return closing
    }
  }
}
