"use client";

import { useCallback, useSyncExternalStore } from "react";

/**
 * 媒体查询订阅 —— 全站「移动端 / 桌面端」结构分叉的唯一判定来源。
 *
 * 为什么需要 JS 判定而不是只写 CSS 断点：绝大多数排版差异（内边距、字号、
 * 网格列数）都该用 Tailwind 的 `max-md:` / `md:` 变体在原地解决，改动最小；
 * 但有两类事情 CSS 做不到，必须靠 JS：
 *   1. **结构分叉**——侧栏在桌面是常驻两栏的一列，在手机是覆盖式抽屉，
 *      DOM 位置与层级完全不同；
 *   2. **只能存在一份的重资源**——侧栏面板是真实 WebGL 液态玻璃
 *      （见 components/glass-panel.tsx），同时渲染「桌面版 + 抽屉版」两份
 *      会白白吃掉一个 WebGL 上下文（浏览器上限很低），必须二选一。
 *
 * 用 useSyncExternalStore 而非 useEffect + useState：订阅外部数据源的标准做法，
 * 服务端快照恒为 false（按桌面渲染），客户端首帧即读到真实值，不会闪一帧错版式。
 */
/**
 * MediaQueryList 按查询串缓存：getSnapshot 在每次渲染（以及 React 校验 store
 * 时）都会被调用，每次都 window.matchMedia() 会不停新建 MQL 对象——海报墙
 * 里几百张卡片同时用 useMediaQuery 时尤其浪费。同一查询串全站共享一个实例。
 */
const mqlCache = new Map<string, MediaQueryList>();

function mediaQueryList(query: string): MediaQueryList {
  let mql = mqlCache.get(query);
  if (!mql) {
    mql = window.matchMedia(query);
    mqlCache.set(query, mql);
  }
  return mql;
}

/**
 * 订阅媒体查询变化。旧版 WebKit 只有已废弃的 addListener/removeListener；
 * 若在启动阶段无条件调用 addEventListener，异常会让整个 AppShell 卸载，页面只剩背景图。
 */
export function subscribeMediaQueryChange(
  mql: MediaQueryList,
  onChange: () => void,
): () => void {
  if (typeof mql.addEventListener === "function") {
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }
  if (typeof mql.addListener === "function") {
    mql.addListener(onChange);
    return () => mql.removeListener(onChange);
  }
  // 极旧 WebView 连旧订阅接口也没有时，保留首次断点判断；旋转后不热切版式，
  // 但工作台仍可使用，刷新后会按新宽度重新计算。
  return () => {};
}

export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (onChange: () => void) => {
      const mql = mediaQueryList(query);
      return subscribeMediaQueryChange(mql, onChange);
    },
    [query],
  );
  return useSyncExternalStore(
    subscribe,
    () => mediaQueryList(query).matches,
    () => false, // SSR：按桌面渲染
  );
}

/** 移动端断点：与 Tailwind 的 md(768px) 对齐——小于它走单栏 + 抽屉式导航。 */
export const MOBILE_QUERY = "(max-width: 767px)";

/** 是否处于移动端版式（< 768px）。 */
export function useIsMobile(): boolean {
  return useMediaQuery(MOBILE_QUERY);
}
