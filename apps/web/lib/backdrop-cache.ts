/** 首帧背景缓存。账号切换时必须清除，避免共享浏览器短暂显示上一账号的背景。 */
export const BACKDROP_CACHE_KEY = "movieclaw.backdrop";

export function clearBackdropCache(): void {
  if (typeof window === "undefined") return;
  try {
    localStorage.removeItem(BACKDROP_CACHE_KEY);
  } catch {
    // localStorage 不可用只会失去首帧优化，不影响服务端的账号隔离。
  }
}
