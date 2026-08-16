"use client";

import { useCallback, useEffect, useRef } from "react";
import type { Route } from "next";
import { useRouter } from "next/navigation";

/** Safari 尚未提供 Navigation API，用会话标记补足「本标签页发生过站内跳转」的判断。 */
const INTERNAL_NAVIGATION_KEY = "movieclaw.internal-navigation";

interface NavigationEntryLike {
  index: number;
  url: string | null;
}

interface NavigationLike {
  currentEntry: NavigationEntryLike | null;
  entries: () => NavigationEntryLike[];
}

/**
 * 在持久存在的 AppShell 中记录客户端路由切换。
 *
 * Chromium 可通过 Navigation API 精确查看上一条历史的来源；Safari 暂无该 API，
 * 因此用 sessionStorage 记住本标签页是否发生过站内路由切换。普通外链直达会清除
 * 旧标记，刷新与前进/后退则保留，避免详情页刷新后丢失真实来路。
 */
export function useAppNavigationTracking(pathname: string) {
  const previousPath = useRef(pathname);
  const initialized = useRef(false);

  useEffect(() => {
    try {
      if (!initialized.current) {
        initialized.current = true;
        const navigation = performance.getEntriesByType("navigation")[0] as
          | PerformanceNavigationTiming
          | undefined;
        if (!navigation || navigation.type === "navigate") {
          sessionStorage.removeItem(INTERNAL_NAVIGATION_KEY);
        }
      } else if (previousPath.current !== pathname) {
        sessionStorage.setItem(INTERNAL_NAVIGATION_KEY, "1");
      }
    } catch {
      // 隐私模式禁用存储时只会失去 Safari 的站内来源识别，仍有 history.length 兜底。
    }
    previousPath.current = pathname;
  }, [pathname]);
}

/** 当前历史中是否存在可安全返回的同源站内页面。 */
function canGoBackWithinApp(): boolean {
  if (window.history.length <= 1) return false;

  const navigation = (window as Window & { navigation?: NavigationLike }).navigation;
  const currentEntry = navigation?.currentEntry;
  if (navigation && currentEntry) {
    const previousEntry = navigation.entries()[currentEntry.index - 1];
    if (!previousEntry?.url) return false;
    try {
      return new URL(previousEntry.url).origin === window.location.origin;
    } catch {
      return false;
    }
  }

  // Safari：同源 document.referrer 覆盖首次进入后的刷新；会话标记覆盖纯 SPA 跳转。
  try {
    if (document.referrer && new URL(document.referrer).origin === window.location.origin) {
      return true;
    }
  } catch {
    // referrer 不是合法 URL 时继续检查会话标记。
  }
  try {
    return sessionStorage.getItem(INTERNAL_NAVIGATION_KEY) === "1";
  } catch {
    return false;
  }
}

/**
 * 统一的详情页后退动作：有同源历史就消费真实上一页；直达时 replace 到结构父级，
 * 不额外制造一条「父页 → 详情页 → 父页」的循环历史。
 */
export function useBackNavigation(fallbackHref: Route) {
  const router = useRouter();
  return useCallback(() => {
    if (canGoBackWithinApp()) {
      router.back();
      return;
    }
    router.replace(fallbackHref, { scroll: false });
  }, [fallbackHref, router]);
}
