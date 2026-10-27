'use strict'

// Electron 桥自测（无显示器也可跑，窗口不显示）：
//   npx electron tests/electron_bridge_check.js
// 覆盖 preload 的 window.desktop（getClientInfo/getSavedLogin/saveLogin/markLoggedOut
// + installUpdate/onUpdateProgress/onCheckUpdateRequest）+ safeStorage 落盘 + 更新下载/拉起。

const { app, BrowserWindow } = require('electron')
const fs = require('node:fs')
const http = require('node:http')
const os = require('node:os')
const path = require('node:path')

const loginStore = require('../login_store')
const updater = require('../updater')
const shellPolicies = require('../shell_policies')

const failures = []

function check(name, ok, detail) {
  if (ok) {
    console.log(`OK: ${name}`)
  } else {
    failures.push(name)
    console.log(`FAIL: ${name}${detail ? ` -> ${detail}` : ''}`)
  }
}

app.whenReady().then(async () => {
  // 独立 userData，避免污染真实配置
  const tempUserData = fs.mkdtempSync(path.join(os.tmpdir(), 'autodroid-bridge-'))
  app.setPath('userData', tempUserData)

  loginStore.registerIpc()

  const win = new BrowserWindow({
    show: false,
    webPreferences: {
      preload: path.join(__dirname, '..', 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  })
  await win.loadURL('data:text/html,<html><body>bridge-check</body></html>')

  const run = (script) => win.webContents.executeJavaScript(script)

  // 1. 桥存在 + getClientInfo 形状
  const hasBridge = await run('!!window.desktop')
  check('window.desktop 已注入', hasBridge === true, JSON.stringify(hasBridge))
  const info = await run('window.desktop.getClientInfo()')
  check(
    'getClientInfo 形状',
    !!info && typeof info.platform === 'string' && typeof info.version === 'string' && Object.keys(info).sort().join(',') === 'platform,version',
    JSON.stringify(info)
  )

  // 2. 空态 getSavedLogin -> null
  const empty = await run('window.desktop.getSavedLogin()')
  check('无保存凭据时返回 null', empty === null, JSON.stringify(empty))

  // 3. saveLogin -> 落盘加密 -> getSavedLogin 回填
  const saved = await run(
    `window.desktop.saveLogin({server: 'http://10.0.0.2:8000/', username: 'alice', password: 'p@ss-明文-秘密', rememberInfo: true, autoLogin: true})`
  )
  check('saveLogin 返回 void', saved === undefined || saved === null, JSON.stringify(saved))
  const roundtrip = await run('window.desktop.getSavedLogin()')
  check(
    'getSavedLogin 回填',
    !!roundtrip &&
      roundtrip.server === 'http://10.0.0.2:8000/' &&
      roundtrip.username === 'alice' &&
      roundtrip.password === 'p@ss-明文-秘密' &&
      roundtrip.rememberInfo === true &&
      roundtrip.autoLogin === true &&
      roundtrip.loggedOut === false,
    JSON.stringify(roundtrip)
  )

  // 4. 落盘文件不含明文密码
  const file = path.join(tempUserData, 'logins.json')
  const raw = fs.readFileSync(file, 'utf-8')
  check('logins.json 存在', fs.existsSync(file))
  check('logins.json 不含明文密码', !raw.includes('p@ss-明文-秘密') && !raw.includes('password:'), raw)
  check('logins.json 含密文', raw.includes('password_enc'), raw)
  check('logins.json 权限 600', (fs.statSync(file).mode & 0o777) === 0o600, (fs.statSync(file).mode & 0o777).toString(8))

  // 4b. 回归：已存在文件被放宽权限后重写，chmod 必须收紧回 600
  //     （writeFileSync 的 mode 只在创建文件时生效）
  fs.chmodSync(file, 0o644)
  await run(
    `window.desktop.saveLogin({server: 'http://10.0.0.2:8000/', username: 'alice', password: 'again', rememberInfo: true, autoLogin: true})`
  )
  check(
    '重写后权限收紧 600',
    (fs.statSync(file).mode & 0o777) === 0o600,
    (fs.statSync(file).mode & 0o777).toString(8)
  )

  // 4c. markLoggedOut（IPC desktop:mark-logged-out）：置 loggedOut、清 autoLogin、保留凭据
  check('markLoggedOut 已暴露', (await run('typeof window.desktop.markLoggedOut')) === 'function')
  await run('window.desktop.markLoggedOut()')
  const afterLogout = await run('window.desktop.getSavedLogin()')
  check(
    'markLoggedOut 后 loggedOut=true 且 autoLogin=false',
    !!afterLogout &&
      afterLogout.loggedOut === true &&
      afterLogout.autoLogin === false &&
      afterLogout.server === 'http://10.0.0.2:8000/' &&
      afterLogout.username === 'alice' &&
      afterLogout.password === 'again',
    JSON.stringify(afterLogout)
  )

  // 4d. saveLogin 重置 loggedOut=false（autoLogin 取传入值）
  await run(
    `window.desktop.saveLogin({server: 'http://10.0.0.2:8000/', username: 'alice', password: 'again', rememberInfo: true, autoLogin: true})`
  )
  const afterResave = await run('window.desktop.getSavedLogin()')
  check(
    'saveLogin 重置 loggedOut=false',
    !!afterResave && afterResave.loggedOut === false && afterResave.autoLogin === true,
    JSON.stringify(afterResave)
  )

  // 5. saveLogin(null) 清除
  await run('window.desktop.saveLogin(null)')
  const cleared = await run('window.desktop.getSavedLogin()')
  check('saveLogin(null) 清除后返回 null', cleared === null, JSON.stringify(cleared))
  check('logins.json 已删除', !fs.existsSync(file))

  // 5b. 无记录时 markLoggedOut no-op（不重建 logins.json）
  await run('window.desktop.markLoggedOut()')
  const stillCleared = await run('window.desktop.getSavedLogin()')
  check('无记录 markLoggedOut no-op', stillCleared === null && !fs.existsSync(file), JSON.stringify(stillCleared))

  // 6. getClientInfo().version 恒等于 package.json version
  //    （electron tests/*.js 启动形态下 app.getVersion() 会退回 Electron 自身版本）
  const pkgVersion = require('../package.json').version
  const infoAgain = await run('window.desktop.getClientInfo()')
  check(
    'getClientInfo().version 等于 package.json version',
    !!infoAgain && infoAgain.version === pkgVersion,
    `got=${infoAgain && infoAgain.version} want=${pkgVersion}`
  )

  // ==================== 更新桥（installUpdate / onUpdateProgress / onCheckUpdateRequest） ====================

  // 模拟安装器拉起：launchMode='fail' 时抛错（失败事件覆盖）
  const launchedPaths = []
  let launchMode = 'ok'
  updater.registerIpc({
    launchInstaller: async (filePath) => {
      if (launchMode === 'fail') {
        throw new Error('无法启动安装程序: mock 拉起失败')
      }
      launchedPaths.push(filePath)
    },
  })

  // 本地 127.0.0.1 更新源（安全红线不变，仅本机回环）：分块慢发保证产生 downloading 进度事件
  const updateServer = http.createServer((req, res) => {
    const urlPath = String(req.url || '').split('?')[0]
    if (urlPath === '/ok' || urlPath === '/second' || urlPath === '/slow') {
      const total = urlPath === '/slow' ? 512 * 1024 : 256 * 1024
      const body = Buffer.alloc(total, 7)
      const chunkSize = 32 * 1024
      const intervalMs = urlPath === '/slow' ? 100 : 80
      res.writeHead(200, { 'Content-Type': 'application/octet-stream', 'Content-Length': String(total) })
      let offset = 0
      const timer = setInterval(() => {
        if (offset >= body.length) {
          clearInterval(timer)
          res.end()
          return
        }
        res.write(body.subarray(offset, offset + chunkSize))
        offset += chunkSize
      }, intervalMs)
      res.on('close', () => clearInterval(timer))
      return
    }
    if (urlPath === '/redirect') {
      res.writeHead(302, { Location: '/ok' })
      res.end()
      return
    }
    res.writeHead(404, { 'Content-Type': 'text/plain', 'Content-Length': '4' })
    res.end('nope')
  })
  await new Promise((resolve) => updateServer.listen(0, '127.0.0.1', resolve))
  const base = `http://127.0.0.1:${updateServer.address().port}`

  // 6a. 桥三方法已暴露
  check('installUpdate 已暴露', (await run('typeof window.desktop.installUpdate')) === 'function')
  check('onUpdateProgress 已暴露', (await run('typeof window.desktop.onUpdateProgress')) === 'function')
  check('onCheckUpdateRequest 已暴露', (await run('typeof window.desktop.onCheckUpdateRequest')) === 'function')

  // 6b. 事件订阅 / 退订
  await run(
    `window.__updEvents = []; window.__updEventsB = [];
     window.__unsubA = window.desktop.onUpdateProgress((e) => window.__updEvents.push(e));
     window.__unsubB = window.desktop.onUpdateProgress((e) => window.__updEventsB.push(e));
     'subscribed'`
  )
  check('onUpdateProgress 返回退订函数', (await run('typeof window.__unsubA')) === 'function')

  const okResult = await run(
    `window.desktop.installUpdate({url: '${base}/ok', fileName: 'AutoDroid Setup 1.2.3.exe', version: '1.2.3'})`
  )
  check('installUpdate 成功返回 {launched:true}', !!okResult && okResult.launched === true, JSON.stringify(okResult))
  await new Promise((resolve) => setTimeout(resolve, 150))

  const downloaded = path.join(tempUserData, 'updates', 'AutoDroid Setup 1.2.3.exe')
  check('安装包落在 userData/updates/<fileName>', fs.existsSync(downloaded) && fs.statSync(downloaded).size === 256 * 1024, downloaded)
  check('成功拉起安装器（mock）', launchedPaths.length === 1 && launchedPaths[0] === downloaded, JSON.stringify(launchedPaths))

  const eventsA = JSON.parse(await run('JSON.stringify(window.__updEvents)'))
  const downloadingEvents = eventsA.filter((e) => e.phase === 'downloading')
  check(
    'downloading 进度事件 percent 0-100 且单调不减',
    downloadingEvents.length >= 1 &&
      downloadingEvents.every((e) => typeof e.percent === 'number' && e.percent >= 0 && e.percent <= 100) &&
      downloadingEvents.every((e, i) => i === 0 || e.percent >= downloadingEvents[i - 1].percent),
    JSON.stringify(downloadingEvents)
  )
  const doneEvent = eventsA.find((e) => e.phase === 'done')
  check('done 事件带 path', !!doneEvent && doneEvent.path === downloaded, JSON.stringify(doneEvent))
  check('成功路径无 error 事件', !eventsA.some((e) => e.phase === 'error'), JSON.stringify(eventsA))

  const unsubReturn = await run('window.__unsubA()')
  check('退订函数可调用且无返回值', unsubReturn === undefined || unsubReturn === null, JSON.stringify(unsubReturn))
  const eventsBBefore = JSON.parse(await run('JSON.stringify(window.__updEventsB)')).length
  const secondResult = await run(
    `window.desktop.installUpdate({url: '${base}/second', fileName: 'AutoDroid-1.2.4-arm64.dmg', version: '1.2.4'})`
  )
  check('退订后不再收到事件', !!secondResult && secondResult.launched === true && JSON.parse(await run('JSON.stringify(window.__updEvents)')).length === eventsA.length, JSON.stringify(secondResult))
  check('未退订订阅仍在收事件', JSON.parse(await run('JSON.stringify(window.__updEventsB)')).length > eventsBBefore)

  // 6c. 托盘「检查更新」请求：onCheckUpdateRequest 订阅 / 退订
  await run(
    `window.__checkReq = 0;
     window.__unsubCheck = window.desktop.onCheckUpdateRequest(() => { window.__checkReq += 1 });
     'subscribed'`
  )
  check('onCheckUpdateRequest 返回退订函数', (await run('typeof window.__unsubCheck')) === 'function')
  updater.notifyCheckUpdateRequested(win)
  await new Promise((resolve) => setTimeout(resolve, 150))
  check('onCheckUpdateRequest 收到检查更新请求', (await run('window.__checkReq')) === 1, String(await run('window.__checkReq')))
  await run('window.__unsubCheck()')
  updater.notifyCheckUpdateRequested(win)
  await new Promise((resolve) => setTimeout(resolve, 150))
  check('onCheckUpdateRequest 退订后不再触发', (await run('window.__checkReq')) === 1, String(await run('window.__checkReq')))

  // 6d. 非法 URL / 文件名拒绝（不产生 error 事件，纯 reject）
  const eventsBeforeBad = JSON.parse(await run('JSON.stringify(window.__updEventsB)')).length
  const badFileUrl = await run(
    `window.desktop.installUpdate({url: 'file:///etc/passwd', fileName: 'x.exe', version: '1.0.0'}).then(() => 'NO_REJECT', (e) => 'ERR:' + e.message)`
  )
  check('file:// URL 拒绝', String(badFileUrl).startsWith('ERR:') && /http/i.test(String(badFileUrl)), String(badFileUrl))
  const badFtpUrl = await run(
    `window.desktop.installUpdate({url: 'ftp://127.0.0.1/x.exe', fileName: 'x.exe', version: '1.0.0'}).then(() => 'NO_REJECT', (e) => 'ERR:' + e.message)`
  )
  check('ftp:// URL 拒绝', String(badFtpUrl).startsWith('ERR:') && /http/i.test(String(badFtpUrl)), String(badFtpUrl))
  const badName = await run(
    `window.desktop.installUpdate({url: '${base}/ok', fileName: '../evil.exe', version: '1.0.0'}).then(() => 'NO_REJECT', (e) => 'ERR:' + e.message)`
  )
  check('带路径分隔符的文件名拒绝', String(badName).startsWith('ERR:') && String(badName).includes('文件名'), String(badName))
  check('参数错误不发 error 事件', JSON.parse(await run('JSON.stringify(window.__updEventsB)')).length === eventsBeforeBad)

  // 6e. 失败事件：HTTP 404 / 拉起安装器失败
  const missing = await run(
    `window.desktop.installUpdate({url: '${base}/missing', fileName: 'missing.exe', version: '9.9.9'}).then(() => 'NO_REJECT', (e) => 'ERR:' + e.message)`
  )
  check('HTTP 404 reject', String(missing).startsWith('ERR:') && String(missing).includes('404'), String(missing))
  const events404 = JSON.parse(await run('JSON.stringify(window.__updEventsB)'))
  check(
    'HTTP 404 error 事件',
    events404.some((e) => e.phase === 'error' && /404/.test(e.message || '')),
    JSON.stringify(events404)
  )
  check('下载失败不留残包', !fs.existsSync(path.join(tempUserData, 'updates', 'missing.exe')))
  check('下载失败不拉起安装器', launchedPaths.length === 2, JSON.stringify(launchedPaths))

  launchMode = 'fail'
  const failLaunch = await run(
    `window.desktop.installUpdate({url: '${base}/ok', fileName: 'fail-launch.exe', version: '1.2.3'}).then(() => 'NO_REJECT', (e) => 'ERR:' + e.message)`
  )
  check('拉起安装器失败 reject', String(failLaunch).startsWith('ERR:') && String(failLaunch).includes('无法启动安装程序'), String(failLaunch))
  const eventsFail = JSON.parse(await run('JSON.stringify(window.__updEventsB)'))
  const lastFail = eventsFail[eventsFail.length - 1]
  check(
    '拉起安装器失败 error 事件',
    !!lastFail && lastFail.phase === 'error' && /无法启动安装程序/.test(lastFail.message || ''),
    JSON.stringify(lastFail)
  )
  launchMode = 'ok'

  // 6f. 下载中重复调用直接 reject，且不影响进行中的下载
  const firstPromise = run(
    `window.desktop.installUpdate({url: '${base}/slow', fileName: 'slow.exe', version: '1.2.3'}).then(() => 'OK', (e) => 'ERR:' + e.message)`
  )
  await new Promise((resolve) => setTimeout(resolve, 300))
  const dup = await run(
    `window.desktop.installUpdate({url: '${base}/slow', fileName: 'slow2.exe', version: '1.2.3'}).then(() => 'NO_REJECT', (e) => 'ERR:' + e.message)`
  )
  check('下载中重复调用直接 reject', String(dup).startsWith('ERR:') && String(dup).includes('进行中'), String(dup))
  const firstDone = await firstPromise
  check('首个下载不受重复调用影响', firstDone === 'OK', String(firstDone))

  // 6g. 302 跟随后仍能完成（下载端点可能重定向到对象存储）
  const redirectResult = await run(
    `window.desktop.installUpdate({url: '${base}/redirect', fileName: 'redirect.exe', version: '1.2.3'}).then(() => 'OK', (e) => 'ERR:' + e.message)`
  )
  check('302 重定向跟随下载成功', redirectResult === 'OK', String(redirectResult))

  // ==================== 壳策略（C1/C2/C3：reload / beforeunload 逃生口 / 引擎自动重启） ====================

  // 7a. Cmd/Ctrl+R 判定
  check('Cmd+R 判定为重新加载', shellPolicies.isReloadShortcut({ key: 'r', meta: true }) === true)
  check('Ctrl+R 判定为重新加载', shellPolicies.isReloadShortcut({ key: 'r', ctrl: true }) === true)
  check('大写 R 同样命中', shellPolicies.isReloadShortcut({ key: 'R', ctrl: true }) === true)
  check('Ctrl+Shift+R（强制刷新变体）命中', shellPolicies.isReloadShortcut({ key: 'r', ctrl: true, shift: true }) === true)
  check('无修饰键的 R 不命中', shellPolicies.isReloadShortcut({ key: 'r' }) === false)
  check('Alt+Ctrl+R 不命中', shellPolicies.isReloadShortcut({ key: 'r', ctrl: true, alt: true }) === false)
  check('Ctrl+Q 不命中', shellPolicies.isReloadShortcut({ key: 'q', ctrl: true }) === false)
  check('空输入不命中', shellPolicies.isReloadShortcut(null) === false)

  // 7b. will-prevent-unload 确认框分支：取消=保留页面 / 确认=强制重载
  let unloadDialogOptions = null
  let unloadChoice = 0
  const fakeDialog = {
    showMessageBoxSync(win, options) {
      unloadDialogOptions = options
      return unloadChoice
    },
  }
  unloadChoice = 0
  check('unload 确认框选「取消」→ 不强制重载', shellPolicies.shouldForceReloadAfterUnloadGuard(fakeDialog, win) === false)
  unloadChoice = 1
  check('unload 确认框选「重新加载」→ 强制重载', shellPolicies.shouldForceReloadAfterUnloadGuard(fakeDialog, win) === true)
  check(
    'unload 确认框文案含未保存提示',
    !!unloadDialogOptions && /未保存/.test(unloadDialogOptions.message),
    JSON.stringify(unloadDialogOptions)
  )
  check(
    'unload 确认框按钮取消在前、默认/取消指针在取消',
    !!unloadDialogOptions &&
      unloadDialogOptions.buttons.join(',') === '取消,重新加载' &&
      unloadDialogOptions.defaultId === 0 &&
      unloadDialogOptions.cancelId === 0,
    JSON.stringify(unloadDialogOptions)
  )

  // 7c. 引擎自动重启退避序列 1s→2s→4s→8s→16s→30s（封顶）与次数上限
  const delays = [1, 2, 3, 4, 5, 6, 7].map((n) => shellPolicies.engineRestartDelayMs(n))
  check(
    '引擎重启指数退避 1s→2s→4s→8s→16s→30s（封顶）',
    JSON.stringify(delays.slice(0, 6)) === JSON.stringify([1000, 2000, 4000, 8000, 16000, 30000]) && delays[6] === 30000,
    JSON.stringify(delays)
  )
  check('重启第 1-5 次允许自动重试', [0, 1, 2, 3, 4, 5].every((n) => shellPolicies.shouldRetryEngineRestart(n) === true))
  check('重启达到上限后不再自动重试', shellPolicies.shouldRetryEngineRestart(6) === false)
  check(
    '稳定运行 30s 后才清失败计数',
    shellPolicies.isEngineRunStable(29999) === false && shellPolicies.isEngineRunStable(30000) === true
  )

  // 7d. 壳接线自检（静态）：main.js 必须挂上 reload 逃生口 / unload 确认 / 引擎自动重启
  const mainSrc = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf-8')
  check('main.js 挂接 will-prevent-unload', mainSrc.includes("'will-prevent-unload'"))
  check('main.js 挂接 before-input-event', mainSrc.includes("'before-input-event'"))
  check('main.js 设置应用菜单（视图→重新加载）', mainSrc.includes('Menu.setApplicationMenu') && mainSrc.includes('重新加载'))
  check(
    'main.js 引擎意外退出自动重启',
    mainSrc.includes('runEngineRestart') && mainSrc.includes('engineRestartDelayMs') && mainSrc.includes('handleEngineUnexpectedExit')
  )

  // 7e. F3：will-prevent-unload 文案按场景区分（重载/退出）
  const reloadOpts = shellPolicies.getUnloadGuardDialogOptions('reload')
  const quitOpts = shellPolicies.getUnloadGuardDialogOptions('quit')
  check(
    '重载场景文案：确定重新加载 + 按钮取消/重新加载',
    /确定重新加载/.test(reloadOpts.message) && reloadOpts.buttons.join(',') === '取消,重新加载',
    JSON.stringify(reloadOpts)
  )
  check(
    '退出场景文案：确定退出 + 按钮取消/退出',
    /确定退出/.test(quitOpts.message) && quitOpts.buttons.join(',') === '取消,退出' && quitOpts.title === '退出',
    JSON.stringify(quitOpts)
  )
  check('未知场景回退重载文案', shellPolicies.getUnloadGuardDialogOptions('unknown').message === reloadOpts.message)
  unloadChoice = 1
  check('退出场景选「退出」→ 放行卸载', shellPolicies.shouldForceReloadAfterUnloadGuard(fakeDialog, win, 'quit') === true)
  check('退出场景确认框按钮为取消/退出', unloadDialogOptions.buttons.join(',') === '取消,退出', JSON.stringify(unloadDialogOptions))
  unloadChoice = 0
  check('退出场景选「取消」→ 不放行', shellPolicies.shouldForceReloadAfterUnloadGuard(fakeDialog, win, 'quit') === false)

  // 7f. F2（C3×C2 竞态）：loadURL ERR_ABORTED = 用户取消重载，不计入重启失败
  check(
    'ERR_ABORTED（message）判为用户取消重载',
    shellPolicies.isUserCancelledReloadError(new Error("ERR_ABORTED (-3) loading 'http://127.0.0.1:1/'")) === true
  )
  check('ERR_ABORTED（code）判为用户取消重载', shellPolicies.isUserCancelledReloadError({ code: 'ERR_ABORTED', message: 'x' }) === true)
  check('errno -3 判为用户取消重载', shellPolicies.isUserCancelledReloadError({ errno: -3, message: 'x' }) === true)
  check(
    '真实失败（ERR_CONNECTION_REFUSED）不判为取消',
    shellPolicies.isUserCancelledReloadError(new Error('ERR_CONNECTION_REFUSED')) === false
  )
  check('空错误不判为取消', shellPolicies.isUserCancelledReloadError(null) === false)

  // 7g. ADB 注入：内置 platform-tools 存在才设 AUTODROID_ADB；已显式设置不覆盖
  const adbCtx = {
    isPackaged: true,
    resourcesPath: '/opt/app/resources',
    appDir: '/repo',
    platform: 'darwin',
    existsSync: () => true,
  }
  check(
    '打包态内置 adb 存在 → 注入 resourcesPath/platform-tools/adb',
    shellPolicies.applyBundledAdbEnv({}, adbCtx).AUTODROID_ADB === '/opt/app/resources/platform-tools/adb'
  )
  check(
    'Windows 内置 adb 带 .exe',
    shellPolicies.applyBundledAdbEnv({}, { ...adbCtx, platform: 'win32' }).AUTODROID_ADB ===
      '/opt/app/resources/platform-tools/adb.exe'
  )
  check(
    '开发态注入分平台目录 assets/platform-tools/darwin/adb',
    shellPolicies.applyBundledAdbEnv({}, { ...adbCtx, isPackaged: false }).AUTODROID_ADB ===
      '/repo/assets/platform-tools/darwin/adb'
  )
  check(
    '开发态 Windows 注入 assets/platform-tools/windows/adb.exe',
    shellPolicies.applyBundledAdbEnv({}, { ...adbCtx, isPackaged: false, platform: 'win32' }).AUTODROID_ADB ===
      '/repo/assets/platform-tools/windows/adb.exe'
  )
  check(
    '开发态分平台目录缺失 → 回退旧扁平布局 assets/platform-tools/adb',
    shellPolicies.applyBundledAdbEnv({}, {
      ...adbCtx,
      isPackaged: false,
      existsSync: (p) => String(p).endsWith(path.join('assets', 'platform-tools', 'adb')),
    }).AUTODROID_ADB === '/repo/assets/platform-tools/adb'
  )
  check(
    '内置 adb 缺失 → 不注入',
    shellPolicies.applyBundledAdbEnv({}, { ...adbCtx, existsSync: () => false }).AUTODROID_ADB === undefined
  )
  check(
    '已显式设置 AUTODROID_ADB 不覆盖',
    shellPolicies.applyBundledAdbEnv({ AUTODROID_ADB: '/user/adb' }, adbCtx).AUTODROID_ADB === '/user/adb'
  )

  // 7h. 壳接线自检（扩展）：F1 记账补排 / F2 ERR_ABORTED 分流 / F3 quitting 复位 / ADB 注入
  check('main.js 重启在途二次退出记账补排（F1）', mainSrc.includes('enginePendingExitReason'))
  check('main.js loadURL ERR_ABORTED 分流（F2）', mainSrc.includes('planEngineRestartTeardown'))
  check('main.js 退出流程取消后复位 quitting（F3）', mainSrc.includes('quitting = false'))
  check('main.js unload 文案按场景区分（F3）', mainSrc.includes("'quit'"))
  check('main.js resolveEngineEnv 注入 AUTODROID_ADB', mainSrc.includes('applyBundledAdbEnv(env'))

  // ==================== G1：引擎生命周期竞态（F1 合并补排 / ERR_ABORTED 不连锁 / stale 忽略） ====================

  // 7i. exit 事件分流（纯函数）：stale 子进程迟到退出必须整体忽略——
  //     不清 engineProcess（抹掉新引擎引用）、不补排重启（制造孤儿引擎）
  const staleChild = { pid: 101 }
  const currentChild = { pid: 202 }
  check(
    'stale 子进程迟到退出 → 整体忽略',
    shellPolicies.planEngineExitAction({ child: staleChild, currentProcess: currentChild, settled: true, quitting: false }) === 'ignore-stale'
  )
  check(
    '启动期（未 settle）退出 → 按启动失败收尾、不走意外退出补排',
    shellPolicies.planEngineExitAction({ child: currentChild, currentProcess: currentChild, settled: false, quitting: false }) === 'startup-failed'
  )
  check(
    '退出流程（quitting）中的退出 → 不排自动重启',
    shellPolicies.planEngineExitAction({ child: currentChild, currentProcess: currentChild, settled: true, quitting: true }) === 'ignore-quitting'
  )
  check(
    '当前引擎意外退出 → 排自动重启',
    shellPolicies.planEngineExitAction({ child: currentChild, currentProcess: currentChild, settled: true, quitting: false }) === 'unexpected-exit'
  )

  // 7j. F1：重启在途二次退出不能丢——失败/二次退出合并成单次补排（同步段内不重复叠排）
  const tFailOnly = shellPolicies.planEngineRestartTeardown({ startedPort: null, error: new Error('spawn 失败'), pendingReason: null })
  check(
    '真实失败 → 续排退避重启',
    tFailOnly.shouldReschedule === true && tFailOnly.failure === 'spawn 失败' && tFailOnly.reason === '自动重启失败: spawn 失败',
    JSON.stringify(tFailOnly)
  )
  const tPendingOnly = shellPolicies.planEngineRestartTeardown({ startedPort: 8123, error: null, pendingReason: 'code=1 signal=none' })
  check(
    '在途二次退出不丢 → 补排一轮',
    tPendingOnly.shouldReschedule === true && tPendingOnly.userCancelled === false && tPendingOnly.reason === 'code=1 signal=none',
    JSON.stringify(tPendingOnly)
  )
  const tMerged = shellPolicies.planEngineRestartTeardown({ startedPort: null, error: new Error('boom'), pendingReason: 'code=1' })
  check(
    '失败+二次退出合并为单次补排、reason 含两者',
    tMerged.shouldReschedule === true && tMerged.reason === '自动重启失败: boom（期间引擎再次退出: code=1）',
    JSON.stringify(tMerged)
  )
  const tClean = shellPolicies.planEngineRestartTeardown({ startedPort: 8123, error: null, pendingReason: null })
  check('成功且无二次退出 → 不排重启', tClean.shouldReschedule === false && tClean.reason === null, JSON.stringify(tClean))

  // 7k. F2：ERR_ABORTED（用户取消重载）不连锁重启——引擎已恢复，取消一次不能重启没完
  const tAborted = shellPolicies.planEngineRestartTeardown({
    startedPort: 8123,
    error: new Error("ERR_ABORTED (-3) loading 'http://127.0.0.1:1/'"),
    pendingReason: null,
  })
  check(
    '用户取消重载 → 不算失败、不连锁重启',
    tAborted.userCancelled === true && tAborted.failure === null && tAborted.shouldReschedule === false,
    JSON.stringify(tAborted)
  )
  const tAbortedWithPending = shellPolicies.planEngineRestartTeardown({
    startedPort: 8123,
    error: { code: 'ERR_ABORTED', message: 'x' },
    pendingReason: 'code=1',
  })
  check(
    '取消重载期间的二次退出仍补排（只按 pending，不算失败）',
    tAbortedWithPending.userCancelled === true && tAbortedWithPending.shouldReschedule === true && tAbortedWithPending.reason === 'code=1',
    JSON.stringify(tAbortedWithPending)
  )
  const tRealLoadFail = shellPolicies.planEngineRestartTeardown({
    startedPort: 8123,
    error: new Error('ERR_CONNECTION_REFUSED'),
    pendingReason: null,
  })
  check(
    '真实 loadURL 失败照旧续排',
    tRealLoadFail.userCancelled === false && tRealLoadFail.failure === 'ERR_CONNECTION_REFUSED' && tRealLoadFail.shouldReschedule === true,
    JSON.stringify(tRealLoadFail)
  )
  check(
    '未启动成功不算用户取消（startedPort 缺席）',
    shellPolicies.planEngineRestartTeardown({ startedPort: null, error: new Error("ERR_ABORTED"), pendingReason: null }).userCancelled === false
  )

  // 7l. G1 接线自检（源码级）：身份校验 / 启动失败丢弃（kill+摘监听）/ quitting 守卫
  const policiesSrc = fs.readFileSync(path.join(__dirname, '..', 'shell_policies.js'), 'utf-8')
  check(
    'main.js exit 处理器走 planEngineExitAction 分流（stale 身份校验）',
    mainSrc.includes('planEngineExitAction(') && policiesSrc.includes('child !== currentProcess')
  )
  check(
    'main.js 启动失败/超时丢弃子进程（kill + 摘监听）',
    (mainSrc.match(/discardEngineChild\(child\)/g) || []).length >= 2 &&
      mainSrc.includes("removeAllListeners('exit')") &&
      mainSrc.includes("removeAllListeners('error')"),
    String((mainSrc.match(/discardEngineChild\(child\)/g) || []).length)
  )
  const guardIdx = mainSrc.indexOf('function handleEngineUnexpectedExit')
  check(
    'main.js handleEngineUnexpectedExit 有 quitting 守卫（退出流程不排重启）',
    guardIdx >= 0 && mainSrc.slice(guardIdx, guardIdx + 240).includes('if (quitting)')
  )
  check(
    'main.js 重启收尾走 planEngineRestartTeardown（F1 合并补排 / F2 不连锁）',
    mainSrc.includes('planEngineRestartTeardown(')
  )

  // 7m. G1 残留：startEngine 顶掉旧引擎前先收尾（kill 旧子进程 + 摘监听），
  //     否则 runEngineRestart 补排的再次 startEngine 会把旧引用覆盖成孤儿引擎
  check(
    'main.js startEngine 顶掉旧引擎前先 kill 旧子进程',
    /function startEngine[\s\S]{0,900}discardEngineChild\(engineProcess\)/.test(mainSrc) &&
      mainSrc.includes("target.kill('SIGKILL')"),
    'startEngine 入口应先 discardEngineChild(engineProcess)'
  )

  updateServer.close()

  win.destroy()
  fs.rmSync(tempUserData, { recursive: true, force: true })

  if (failures.length) {
    console.log(`BRIDGE CHECK FAILED (${failures.length})`)
    app.exit(1)
  }
  console.log('BRIDGE CHECK PASSED')
  app.exit(0)
}).catch((error) => {
  console.error('BRIDGE CHECK ERROR:', error)
  app.exit(1)
})
