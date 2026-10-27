'use strict'

// 保存登录信息：密码用 Electron safeStorage 加密存 userData/logins.json
// （base64 密文），明文绝不写盘；null = 清除。
const { app, ipcMain, safeStorage } = require('electron')
const fs = require('node:fs')
const path = require('node:path')

const LOGINS_FILE = 'logins.json'
// 契约要求 getClientInfo().version 恒等于 package.json version。
// app.getVersion() 在 `electron tests/*.js` 这类无 package.json 的启动形态下
// 会退回 Electron 自身版本，故显式读 package.json。
const PACKAGE_VERSION = require('./package.json').version

function loginsPath() {
  return path.join(app.getPath('userData'), LOGINS_FILE)
}

function readSavedLogin() {
  let raw
  try {
    raw = JSON.parse(fs.readFileSync(loginsPath(), 'utf-8'))
  } catch (error) {
    return null
  }
  if (!raw || typeof raw !== 'object') {
    return null
  }
  let password = ''
  if (raw.password_enc) {
    try {
      password = safeStorage.decryptString(Buffer.from(String(raw.password_enc), 'base64'))
    } catch (error) {
      process.stderr.write(`[logins] 解密失败，忽略已存密码: ${error.message}\n`)
      password = ''
    }
  }
  return {
    server: String(raw.server || ''),
    username: String(raw.username || ''),
    password,
    rememberInfo: !!raw.rememberInfo,
    autoLogin: !!raw.autoLogin,
    loggedOut: !!raw.loggedOut,
  }
}

function buildPayload(data, loggedOut) {
  const password = String(data.password || '')
  let passwordEnc = ''
  if (password) {
    if (!safeStorage.isEncryptionAvailable()) {
      throw new Error('系统安全存储不可用，无法加密保存密码')
    }
    passwordEnc = safeStorage.encryptString(password).toString('base64')
  }
  return {
    server: String(data.server || ''),
    username: String(data.username || ''),
    rememberInfo: !!data.rememberInfo,
    autoLogin: !!data.autoLogin,
    loggedOut: !!loggedOut,
    password_enc: passwordEnc,
  }
}

function persistPayload(payload) {
  const file = loginsPath()
  fs.mkdirSync(path.dirname(file), { recursive: true })
  fs.writeFileSync(file, JSON.stringify(payload, null, 2), { mode: 0o600 })
  // writeFileSync 的 mode 只在创建文件时生效；已存在文件（含被放宽过权限的）
  // 必须显式 chmod 收紧到 600。
  try {
    fs.chmodSync(file, 0o600)
  } catch (error) {
    process.stderr.write(`[logins] 收紧文件权限失败: ${error.message}\n`)
  }
}

function writeSavedLogin(data) {
  if (data === null || data === undefined) {
    try {
      fs.unlinkSync(loginsPath())
    } catch (error) {
      // 文件不存在视为清除成功
    }
    return
  }
  // 重新保存（含自动登录）视为回到登录态：loggedOut 一律复位为 false。
  persistPayload(buildPayload(data, false))
}

function markLoggedOut() {
  // 手动退出登录：置 loggedOut=true 且 autoLogin=false（保留 server/username/password）；
  // 无保存记录则 no-op。
  const saved = readSavedLogin()
  if (!saved) {
    return
  }
  persistPayload(buildPayload({ ...saved, autoLogin: false }, true))
}

function registerIpc() {
  ipcMain.handle('desktop:get-client-info', () => ({
    platform: process.platform,
    version: PACKAGE_VERSION,
  }))
  ipcMain.handle('desktop:get-saved-login', () => readSavedLogin())
  ipcMain.handle('desktop:save-login', (_event, data) => {
    writeSavedLogin(data)
  })
  ipcMain.handle('desktop:mark-logged-out', () => {
    markLoggedOut()
  })
}

module.exports = { loginsPath, readSavedLogin, writeSavedLogin, markLoggedOut, registerIpc }
