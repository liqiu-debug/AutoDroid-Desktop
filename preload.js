'use strict'

// window.desktop 桥：客户端模式能力（无此对象 = 纯 Web 行为，前端需优雅降级）
// 密码只在主进程用 safeStorage 加密落盘，preload 不接触文件系统。
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('desktop', {
  /** @returns {Promise<{platform: string, version: string}>} */
  getClientInfo: () => ipcRenderer.invoke('desktop:get-client-info'),

  /** @returns {Promise<{server: string, username: string, password: string, rememberInfo: boolean, autoLogin: boolean, loggedOut: boolean} | null>} */
  getSavedLogin: () => ipcRenderer.invoke('desktop:get-saved-login'),

  /** @param {{server: string, username: string, password: string, rememberInfo: boolean, autoLogin: boolean} | null} data */
  saveLogin: (data) => ipcRenderer.invoke('desktop:save-login', data),

  /** @returns {Promise<void>} */
  markLoggedOut: () => ipcRenderer.invoke('desktop:mark-logged-out'),

  /**
   * 下载更新包到 userData/updates/<fileName>（断点续传 + 大小校验）并按平台安装：
   * - macOS：挂载校验 + 同卷暂存后返回 {ready:true}，由 applyUpdate 换包重启；
   * - win32：拉起安装向导后返回 {launched:true}。
   * 过程事件经 onUpdateProgress 订阅：
   * {phase:'downloading',percent} | {phase:'ready',version,path} | {phase:'downloaded',path,message}
   * | {phase:'done',path} | {phase:'error',message}。
   * @param {{url: string, fileName: string, version?: string, expectedSize?: number, autoInstall?: boolean}} payload
   * @returns {Promise<{launched: boolean, ready?: boolean, version?: string}>} 失败 reject Error(message)
   */
  installUpdate: async (payload) => {
    const result = await ipcRenderer.invoke('desktop:install-update', payload)
    if (result && result.ok === false) {
      throw new Error(result.message || '更新失败')
    }
    return result
  },

  /**
   * 查询更新源最新版本（GitHub Releases，公开仓库无需 token）。
   * @returns {Promise<{ok: true, release: {version: string, fileName: string, url: string, size: number, notes: string}} | {ok: false, message: string}>}
   */
  checkRelease: () => ipcRenderer.invoke('desktop:check-release'),

  /**
   * 应用已暂存的更新：替换应用包并重启（无参数，壳按自身记录执行）。
   * @returns {Promise<{ok: boolean, message?: string}>}
   */
  applyUpdate: () => ipcRenderer.invoke('desktop:apply-update'),

  /**
   * 查询是否有已下载/已暂存的更新（重启入口复用：无需重新下载）。
   * @returns {Promise<{staged: {version: string, fileName: string, size: number} | null}>}
   */
  getUpdateStatus: () => ipcRenderer.invoke('desktop:update-status'),

  /**
   * 包管理下载：断点续传下载任意安装包到系统「下载」目录并定位文件。
   * 过程事件经 onDownloadProgress 订阅：{phase:'downloading',percent} | {phase:'done',path,size} | {phase:'error',message}。
   * @param {{url: string, fileName: string, expectedSize?: number}} payload
   * @returns {Promise<{path: string, size: number}>} 失败 reject Error(message)
   */
  downloadPackage: async (payload) => {
    const result = await ipcRenderer.invoke('desktop:download-package', payload)
    if (result && result.ok === false) {
      throw new Error(result.message || '下载失败')
    }
    return result
  },

  /**
   * 订阅包下载过程事件（desktop:download-package 专用通道）。
   * @param {(event: {phase: 'downloading', percent: number} | {phase: 'done', path: string, size: number} | {phase: 'error', message: string}) => void} cb
   * @returns {() => void} 退订函数
   */
  onDownloadProgress: (cb) => {
    if (typeof cb !== 'function') {
      throw new TypeError('onDownloadProgress 需要函数参数')
    }
    const handler = (_event, payload) => cb(payload)
    ipcRenderer.on('autodroid:download-progress', handler)
    return () => {
      ipcRenderer.removeListener('autodroid:download-progress', handler)
    }
  },

  /**
   * 订阅更新过程事件。
   * @param {(event: {phase: 'downloading', percent: number} | {phase: 'done', path: string} | {phase: 'error', message: string}) => void} cb
   * @returns {() => void} 退订函数
   */
  onUpdateProgress: (cb) => {
    if (typeof cb !== 'function') {
      throw new TypeError('onUpdateProgress 需要函数参数')
    }
    const handler = (_event, payload) => cb(payload)
    ipcRenderer.on('autodroid:update-progress', handler)
    // 注意：removeListener 返回 EventEmitter 本身，跨 contextBridge 不可克隆，
    // 退订函数必须显式返回 undefined。
    return () => {
      ipcRenderer.removeListener('autodroid:update-progress', handler)
    }
  },

  /**
   * 订阅「检查更新」请求（托盘菜单触发），前端走同一套检查流程。
   * @param {() => void} cb
   * @returns {() => void} 退订函数
   */
  onCheckUpdateRequest: (cb) => {
    if (typeof cb !== 'function') {
      throw new TypeError('onCheckUpdateRequest 需要函数参数')
    }
    const handler = () => cb()
    ipcRenderer.on('autodroid:check-update', handler)
    return () => {
      ipcRenderer.removeListener('autodroid:check-update', handler)
    }
  },
})
