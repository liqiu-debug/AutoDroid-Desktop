'use strict'

// login_store 单测（纯 Node，mock electron，不启动 Electron）：
//   node tests/login_store_check.js
// 覆盖 loggedOut 持久化语义：
//   - readSavedLogin 返回 loggedOut（旧记录缺省 false）
//   - markLoggedOut 置 loggedOut=true 且 autoLogin=false，保留 server/username/password
//   - writeSavedLogin 重置 loggedOut=false（autoLogin 取传入值）
//   - 无保存记录时 markLoggedOut no-op

const assert = require('node:assert')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const Module = require('node:module')

const tempUserData = fs.mkdtempSync(path.join(os.tmpdir(), 'autodroid-login-store-'))

const fakeElectron = {
  app: {
    getPath: () => tempUserData,
  },
  ipcMain: {
    handle: () => {},
  },
  // 可逆 mock：encrypt/decrypt 互逆即可，只测字段语义（真实加密见 electron_bridge_check.js）
  safeStorage: {
    isEncryptionAvailable: () => true,
    encryptString: (text) => Buffer.from(String(text), 'utf-8'),
    decryptString: (buf) => Buffer.from(buf).toString('utf-8'),
  },
}

const origLoad = Module._load
Module._load = function (request, parent, isMain) {
  if (request === 'electron') {
    return fakeElectron
  }
  return origLoad(request, parent, isMain)
}

const loginStore = require('../login_store')
Module._load = origLoad

const failures = []

function check(name, ok, detail) {
  if (ok) {
    console.log(`OK: ${name}`)
  } else {
    failures.push(name)
    console.log(`FAIL: ${name}${detail ? ` -> ${detail}` : ''}`)
  }
}

function resetStore() {
  try {
    fs.rmSync(loginStore.loginsPath(), { force: true })
  } catch (error) {
    // 文件不存在即可
  }
}

try {
  // 1. 无保存记录：markLoggedOut no-op（不建文件）
  resetStore()
  loginStore.markLoggedOut()
  check('markLoggedOut 无记录 no-op', !fs.existsSync(loginStore.loginsPath()) && loginStore.readSavedLogin() === null)

  // 2. writeSavedLogin 写入 loggedOut=false，autoLogin 取传入值
  loginStore.writeSavedLogin({
    server: 'http://127.0.0.1:8000/',
    username: 'alice',
    password: 'pw-秘密',
    rememberInfo: true,
    autoLogin: true,
  })
  let saved = loginStore.readSavedLogin()
  check('writeSavedLogin 后 loggedOut=false', saved && saved.loggedOut === false, JSON.stringify(saved))
  check('writeSavedLogin 后 autoLogin=true', saved && saved.autoLogin === true, JSON.stringify(saved))

  // 3. markLoggedOut：置 loggedOut=true 且 autoLogin=false，保留 server/username/password
  loginStore.markLoggedOut()
  saved = loginStore.readSavedLogin()
  check('markLoggedOut 置 loggedOut=true', saved && saved.loggedOut === true, JSON.stringify(saved))
  check('markLoggedOut 清 autoLogin', saved && saved.autoLogin === false, JSON.stringify(saved))
  check(
    'markLoggedOut 保留 server/username/password',
    saved &&
      saved.server === 'http://127.0.0.1:8000/' &&
      saved.username === 'alice' &&
      saved.password === 'pw-秘密',
    JSON.stringify(saved)
  )

  // 4. writeSavedLogin 重置 loggedOut=false（autoLogin 取传入值）
  loginStore.writeSavedLogin({
    server: 'http://127.0.0.1:8000/',
    username: 'alice',
    password: 'pw-秘密',
    rememberInfo: true,
    autoLogin: true,
  })
  saved = loginStore.readSavedLogin()
  check('writeSavedLogin 重置 loggedOut=false', saved && saved.loggedOut === false && saved.autoLogin === true, JSON.stringify(saved))

  // 5. 旧版 logins.json（无 loggedOut 字段）→ readSavedLogin 补 false
  resetStore()
  fs.mkdirSync(path.dirname(loginStore.loginsPath()), { recursive: true })
  fs.writeFileSync(
    loginStore.loginsPath(),
    JSON.stringify({ server: 'http://127.0.0.1:8000/', username: 'bob', rememberInfo: true, autoLogin: true, password_enc: '' })
  )
  saved = loginStore.readSavedLogin()
  check('旧记录缺省 loggedOut=false', saved && saved.loggedOut === false, JSON.stringify(saved))

  // 6. 清除后再 markLoggedOut：仍然 no-op
  loginStore.writeSavedLogin(null)
  loginStore.markLoggedOut()
  check('清除后 markLoggedOut no-op', loginStore.readSavedLogin() === null && !fs.existsSync(loginStore.loginsPath()))

  // 7. registerIpc 登记 desktop:mark-logged-out 通道
  const channels = []
  const origHandle = fakeElectron.ipcMain.handle
  fakeElectron.ipcMain.handle = (channel, fn) => {
    channels.push(channel)
  }
  loginStore.registerIpc()
  fakeElectron.ipcMain.handle = origHandle
  check(
    'registerIpc 登记 desktop:mark-logged-out',
    channels.includes('desktop:mark-logged-out'),
    JSON.stringify(channels)
  )
  check(
    'registerIpc 登记既有通道',
    ['desktop:get-client-info', 'desktop:get-saved-login', 'desktop:save-login'].every((c) => channels.includes(c)),
    JSON.stringify(channels)
  )
} finally {
  fs.rmSync(tempUserData, { recursive: true, force: true })
}

if (failures.length) {
  console.log(`LOGIN STORE CHECK FAILED (${failures.length})`)
  process.exit(1)
}
console.log('LOGIN STORE CHECK PASSED')
