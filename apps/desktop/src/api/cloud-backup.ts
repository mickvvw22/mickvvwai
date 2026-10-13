import { hermesApi, profileScoped } from './client'

export interface WebDAVConnection {
  url: string
  username: string
  password: string
}

export interface WebDAVBackupFile {
  filename: string
  size: number
  modified: string
}

export interface WebDAVBackupResponse {
  ok: boolean
  filename: string
  size?: number
  restored?: boolean
}

function scope(profile?: null | string) {
  // The backend requires an explicit target for destructive profile actions.
  return profileScoped(profile || 'default')
}

export function testWebDAV(connection: WebDAVConnection, profile?: null | string) {
  return hermesApi<{ ok: boolean; status: number }>({
    ...scope(profile),
    path: '/api/webdav/test',
    method: 'POST',
    body: connection
  })
}

export function listWebDAVBackups(connection: WebDAVConnection, profile?: null | string) {
  return hermesApi<{ files: WebDAVBackupFile[] }>({
    ...scope(profile),
    path: '/api/webdav/list',
    method: 'POST',
    body: connection
  })
}

export function createWebDAVBackup(connection: WebDAVConnection, passphrase: string, profile?: null | string) {
  return hermesApi<WebDAVBackupResponse>({
    ...scope(profile),
    path: '/api/webdav/create',
    method: 'POST',
    body: { ...connection, passphrase },
    timeoutMs: 300_000
  })
}

export function restoreWebDAVBackup(
  connection: WebDAVConnection,
  passphrase: string,
  filename: string,
  profile?: null | string
) {
  return hermesApi<WebDAVBackupResponse>({
    ...scope(profile),
    path: '/api/webdav/restore',
    method: 'POST',
    body: { ...connection, passphrase, filename },
    timeoutMs: 600_000
  })
}
