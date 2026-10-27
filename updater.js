'use strict'

// 应用内「检查更新 / 下载安装」（更新源 = 平台 App 包管理，与 Android 客户端同一通道）：
// - desktop:install-update：按 URL 流式下载到 userData/updates/<fileName>，
//   过程/结果事件（autodroid:update-progress）推给渲染进程，下载完成后拉起安装器。
// - 托盘「检查更新」经 autodroid:check-update 通知渲染进程，走同一套检查流程。
// 壳不存储 token：下载 URL 由前端（含 query token）现拼现用，不落盘、不缓存。
const { app, ipcMain, shell } = require('electron')
const { spawn } = require('node:child_process')
const fs = require('node:fs')
const http = require('node:http')
const https = require('node:https')
const path = require('node:path')
const { pipeline } = require('node:stream/promises')

const PROGRESS_CHANNEL = 'autodroid:update-progress'
const CHECK_UPDATE_CHANNEL = 'autodroid:check-update'
// 进度事件节流间隔（下载过程事件 ~200ms 一发）
const PROGRESS_THROTTLE_MS = 200
// socket 空闲超时：连接/收流长时间无数据即中断，归一为 error 事件
const IDLE_TIMEOUT_MS = 30000
const MAX_REDIRECTS = 5

// 同一时刻只允许一个下载任务；下载中重复调用直接 reject
let downloading = false

function updatesDir() {
  return path.join(app.getPath('userData'), 'updates')
}

// 只允许 http(s) 下载地址（file:/ftp:/javascript: 等一律拒绝）
function isHttpUrl(raw) {
  let parsed
  try {
    parsed = new URL(String(raw))
  } catch (error) {
    return false
  }
  return parsed.protocol === 'http:' || parsed.protocol === 'https:'
}

// 安装包文件名：不允许带路径分隔符 / 目录穿越，仅取纯文件名
function isSafeFileName(raw) {
  const name = String(raw || '')
  return name !== '' && name !== '.' && name !== '..' && !/[/\\]/.test(name)
}

function errorMessage(error, fallback) {
  const message = String((error && error.message) || error || '')
  return message || fallback
}

// 跟随有限次重定向（下载端点可能 302 到对象存储），每一跳仍须 http(s)
function getResponse(url, redirectsLeft) {
  return new Promise((resolve, reject) => {
    if (!isHttpUrl(url)) {
      reject(new Error('仅支持 http(s) 下载地址'))
      return
    }
    const parsed = new URL(String(url))
    const mod = parsed.protocol === 'https:' ? https : http
    const req = mod.get(parsed, { timeout: IDLE_TIMEOUT_MS }, (res) => {
      const status = res.statusCode || 0
      if (status >= 300 && status < 400 && res.headers.location) {
        res.resume()
        if (redirectsLeft <= 0) {
          reject(new Error('下载失败（重定向次数过多）'))
          return
        }
        let next
        try {
          next = new URL(res.headers.location, parsed).toString()
        } catch (error) {
          reject(new Error('下载失败（重定向地址非法）'))
          return
        }
        resolve(getResponse(next, redirectsLeft - 1))
        return
      }
      if (status !== 200) {
        res.resume()
        reject(new Error(`下载失败（HTTP ${status}）`))
        return
      }
      resolve(res)
    })
    req.on('timeout', () => {
      req.destroy(new Error('下载超时'))
    })
    req.on('error', (error) => {
      reject(new Error(errorMessage(error, '下载失败')))
    })
  })
}

async function downloadTo(dest, url, onProgress) {
  const res = await getResponse(url, MAX_REDIRECTS)
  const total = Number(res.headers['content-length']) || 0
  let received = 0
  let lastEmit = 0
  res.on('data', (chunk) => {
    received += chunk.length
    const now = Date.now()
    if (now - lastEmit >= PROGRESS_THROTTLE_MS) {
      lastEmit = now
      const percent = total > 0 ? Math.min(100, Math.max(0, Math.round((received / total) * 100))) : 0
      onProgress(percent)
    }
  })
  try {
    await pipeline(res, fs.createWriteStream(dest))
  } catch (error) {
    throw new Error(errorMessage(error, '下载失败'))
  }
  if (total > 0 && received !== total) {
    throw new Error(`下载不完整（${received}/${total} 字节）`)
  }
}

// 拉起安装器：win32 spawn(NSIS 向导)；darwin shell.openPath(dmg，Finder 打开)
function defaultLaunchInstaller(filePath) {
  if (process.platform === 'win32') {
    return new Promise((resolve, reject) => {
      let settled = false
      let child
      try {
        child = spawn(filePath, [], { detached: true, stdio: 'ignore' })
      } catch (error) {
        reject(new Error(`无法启动安装程序: ${errorMessage(error, 'spawn 失败')}`))
        return
      }
      child.on('error', (error) => {
        if (!settled) {
          settled = true
          reject(new Error(`无法启动安装程序: ${errorMessage(error, 'spawn 失败')}`))
        }
      })
      child.on('spawn', () => {
        if (!settled) {
          settled = true
          try {
            child.unref()
          } catch (error) {
            // 进程可能已退出
          }
          resolve()
        }
      })
    })
  }
  if (process.platform === 'darwin') {
    return shell.openPath(filePath).then((errorMessageText) => {
      if (errorMessageText) {
        throw new Error(`无法打开安装包: ${errorMessageText}`)
      }
    })
  }
  return Promise.reject(new Error('当前平台不支持自动安装更新'))
}

// payload：{ url, fileName, version }（version 仅前端记账用，壳不消费）
async function installUpdate(sender, payload, launchInstaller) {
  const data = payload && typeof payload === 'object' ? payload : {}
  const url = data.url
  const fileName = data.fileName
  if (!isHttpUrl(url)) {
    throw new Error('仅支持 http(s) 下载地址')
  }
  if (!isSafeFileName(fileName)) {
    throw new Error('非法的安装包文件名')
  }
  if (downloading) {
    throw new Error('已有下载任务进行中')
  }
  downloading = true
  const emit = (event) => {
    try {
      sender.send(PROGRESS_CHANNEL, event)
    } catch (error) {
      // 窗口已关闭等场景：事件丢失无碍，主流程照常收尾
    }
  }
  const dest = path.join(updatesDir(), fileName)
  let downloaded = false
  try {
    fs.mkdirSync(updatesDir(), { recursive: true })
    await downloadTo(dest, url, (percent) => emit({ phase: 'downloading', percent }))
    downloaded = true
    await launchInstaller(dest)
    // 下载完成并已拉起安装器
    emit({ phase: 'done', path: dest })
    return { launched: true }
  } catch (error) {
    // 下载失败清掉残包；已完整下载但拉起失败的保留文件（可手动安装）
    if (!downloaded) {
      try {
        fs.unlinkSync(dest)
      } catch (cleanupError) {
        // 未产生文件时忽略
      }
    }
    const message = errorMessage(error, '更新失败')
    emit({ phase: 'error', message })
    throw new Error(message)
  } finally {
    downloading = false
  }
}

function notifyCheckUpdateRequested(win) {
  if (win && !win.isDestroyed()) {
    win.webContents.send(CHECK_UPDATE_CHANNEL)
  }
}

// options.launchInstaller 可注入（桥自测 mock 拉起分支），默认按平台拉起安装器。
// handler 不抛异常（否则 Electron 会往 stderr 打 "Error occurred in handler" 堆栈），
// 失败以 {ok:false,message} 回给 preload，由 preload reject Error(message)——渲染进程
// 侧契约不变：成功 resolve {launched:true}，失败 reject Error(message)。
function registerIpc(options) {
  const launchInstaller = (options && options.launchInstaller) || defaultLaunchInstaller
  ipcMain.handle('desktop:install-update', async (event, payload) => {
    try {
      return await installUpdate(event.sender, payload, launchInstaller)
    } catch (error) {
      return { ok: false, message: errorMessage(error, '更新失败') }
    }
  })
}

module.exports = {
  CHECK_UPDATE_CHANNEL,
  PROGRESS_CHANNEL,
  defaultLaunchInstaller,
  installUpdate,
  notifyCheckUpdateRequested,
  registerIpc,
  updatesDir,
}
