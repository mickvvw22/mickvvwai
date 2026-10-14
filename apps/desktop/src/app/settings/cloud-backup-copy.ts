export interface CloudBackupCopy {
  title: string
  intro: string
  warning: string
  destination: string
  url: string
  username: string
  password: string
  passphrase: string
  passphraseHint: string
  testConnection: string
  refreshList: string
  createBackup: string
  backups: string
  empty: string
  restore: string
  restoreTitle: string
  restoreConfirm: string
  cancel: string
  size: string
  modified: string
  connected: string
  uploaded: string
  restored: string
  loading: string
  error: string
  required: string
}

const zh: CloudBackupCopy = {
  title: '云端备份',
  intro: '连接 WebDAV 服务器，创建加密备份，并在需要时恢复到当前所选配置。',
  warning: '备份包含配置、凭据、会话数据，以及归档中的共享外部提供商状态。临时归档只在验证过的内存文件系统中处理；若不可用会停止，不会回退到普通磁盘。上传前使用 AES-GCM 加密；加密口令不会保存。请使用至少 12 个字符的强口令并妥善保管，遗失后无法恢复备份。',
  destination: 'WebDAV 连接',
  url: 'WebDAV 文件夹 URL',
  username: '用户名（可选）',
  password: '密码（可选）',
  passphrase: '备份加密口令',
  passphraseHint: '至少 12 个字符。恢复备份时必须使用同一口令。',
  testConnection: '测试连接',
  refreshList: '刷新列表',
  createBackup: '立即备份',
  backups: '远端备份',
  empty: '未找到此文件夹中的加密备份。',
  restore: '恢复',
  restoreTitle: '恢复此云端备份？',
  restoreConfirm: '这将覆盖当前所选配置中的同名文件；若归档包含共享外部提供商状态，也会将其写回用户主目录。继续前会创建本地回滚备份；请确认选中的配置范围正确。',
  cancel: '取消',
  size: '大小',
  modified: '修改时间',
  connected: 'WebDAV 连接成功。',
  uploaded: '加密备份已上传：',
  restored: '备份已恢复。',
  loading: '处理中…',
  error: '操作失败',
  required: '请填写 WebDAV URL 和至少 12 个字符的加密口令。'
}

const zhHant: CloudBackupCopy = {
  ...zh,
  title: '雲端備份',
  intro: '連線至 WebDAV 伺服器，建立加密備份，並在需要時還原至目前選取的設定。',
  warning: '備份包含設定、憑證、工作階段資料，以及封存中的共用外部提供者狀態。暫存封存只會在經驗證的記憶體檔案系統中處理；若不可用便會停止，不會改用一般磁碟。上傳前會以 AES-GCM 加密；加密密語不會儲存。請使用至少 12 個字元的強密語並妥善保管，遺失後無法還原備份。',
  destination: 'WebDAV 連線',
  url: 'WebDAV 資料夾 URL',
  username: '使用者名稱（選填）',
  password: '密碼（選填）',
  passphrase: '備份加密密語',
  passphraseHint: '至少 12 個字元。還原備份時必須使用相同密語。',
  testConnection: '測試連線',
  refreshList: '重新整理清單',
  createBackup: '立即備份',
  backups: '遠端備份',
  empty: '此資料夾中找不到加密備份。',
  restore: '還原',
  restoreTitle: '要還原此雲端備份嗎？',
  restoreConfirm: '這會覆寫目前所選設定中的同名檔案；若封存含有共用外部提供者狀態，也會將其寫回使用者主目錄。繼續前會建立本機回復備份；請確認選取的設定範圍正確。',
  cancel: '取消',
  connected: 'WebDAV 連線成功。',
  uploaded: '加密備份已上傳：',
  restored: '備份已還原。',
  loading: '處理中…',
  error: '操作失敗',
  required: '請填寫 WebDAV URL 和至少 12 個字元的加密密語。'
}

const en: CloudBackupCopy = {
  title: 'Cloud backup',
  intro: 'Connect a WebDAV server to create encrypted backups and restore them to the currently selected profile.',
  warning: 'Backups include configuration, credentials, session data, and any shared external-provider state in the archive. Temporary archives are processed only on a verified RAM-backed filesystem; if one is unavailable, the operation stops instead of falling back to disk. Data is encrypted with AES-GCM before upload; the passphrase is never saved. Use a strong passphrase of at least 12 characters and keep it safe—lost passphrases cannot be recovered.',
  destination: 'WebDAV connection',
  url: 'WebDAV folder URL',
  username: 'Username (optional)',
  password: 'Password (optional)',
  passphrase: 'Backup encryption passphrase',
  passphraseHint: 'At least 12 characters. The same passphrase is required to restore.',
  testConnection: 'Test connection',
  refreshList: 'Refresh list',
  createBackup: 'Back up now',
  backups: 'Remote backups',
  empty: 'No encrypted backups found in this folder.',
  restore: 'Restore',
  restoreTitle: 'Restore this cloud backup?',
  restoreConfirm: 'This overwrites matching files in the currently selected profile. If the archive contains shared external-provider state, that is also restored to the user home. A local rollback backup is created first; make sure the selected profile is correct.',
  cancel: 'Cancel',
  size: 'Size',
  modified: 'Modified',
  connected: 'WebDAV connection succeeded.',
  uploaded: 'Encrypted backup uploaded:',
  restored: 'Backup restored.',
  loading: 'Working…',
  error: 'Operation failed',
  required: 'Enter a WebDAV URL and an encryption passphrase of at least 12 characters.'
}

export function getCloudBackupCopy(locale: string): CloudBackupCopy {
  if (locale === 'zh') return zh
  if (locale === 'zh-hant') return zhHant
  return en
}
